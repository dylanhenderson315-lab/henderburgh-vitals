"""The channel guide: every HTC channel, six hours ahead, in one grid.

Listings come from two places and are merged per channel:
  - TVmaze's US schedule (today + tomorrow, so after-midnight shows land) —
    each episode has an exact airstamp and runtime; its network name maps to
    an HTC channel through services/htc.py.
  - ESPN's games (services/watch.tonight) for the sports channels, with a
    league-typical length; a live game always runs until at least now + 30.
Where both claim the same slot on a channel, the game wins (TVmaze lists
"College Football" filler that the real game should replace).

Rules: never invent a programme — a channel with nothing listed gets an
honest empty row the owner can still tap to tune; times are exact or, for
games, labelled as estimates by the client.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from services import htc
from services.watch import ET, _cached, _store, apps_for

UA = {"User-Agent": "henderburgh-hub/1.0"}
GAME_MIN = {"NFL": 195, "NCAAF": 210, "NBA": 150, "NHL": 150, "MLB": 180, "UFC": 240, "BOXING": 240}
HOURS = 6
# Networks whose shows really are in the owner's apps (next-day or live).
# Channel-level guesses beyond these would be wrong (TBS sitcoms aren't on Max).
CHANNEL_APPS = {10: ["peacock"], 65: ["peacock"], 13: ["paramount"], 15: ["hulu"], 5: ["hulu"], 60: ["hulu"], 72: ["hulu"]}


def _dt(iso: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return None


async def _tvmaze_day(client: httpx.AsyncClient, day: str) -> List[Dict[str, Any]]:
    key = f"tvm-grid:{day}"
    hit = _cached(key, 900)
    if hit is not None:
        return hit
    try:
        r = await client.get("https://api.tvmaze.com/schedule", params={"country": "US", "date": day})
        r.raise_for_status()
        out = r.json() or []
    except Exception as e:
        print(f"guide tvmaze {day} error: {e}")
        out = []
    return _store(key, out)


def _shows(eps: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    by: Dict[int, List[Dict[str, Any]]] = {}
    for e in eps:
        show = e.get("show") or {}
        net = (show.get("network") or {}).get("name")
        ch = htc.channel_for(net)
        start = _dt(e.get("airstamp"))
        if not ch or not start:
            continue
        mins = e.get("runtime") or show.get("runtime") or 30
        genres = show.get("genres") or []
        sub = e.get("name") if e.get("name") and e.get("name") != show.get("name") else None
        by.setdefault(ch, []).append({
            "start": start.isoformat(), "end": (start + timedelta(minutes=int(mins))).isoformat(),
            "title": show.get("name") or "Untitled", "sub": sub, "kind": "show",
            "genre": genres[0] if genres else None, "apps": apps_for([net], sports=False) if net else [],
        })
    return by


def _games(games: List[Dict[str, Any]], now: datetime) -> Dict[int, List[Dict[str, Any]]]:
    by: Dict[int, List[Dict[str, Any]]] = {}
    for g in games:
        start = _dt(g.get("start"))
        if not start or g.get("state") == "post":
            continue
        end = start + timedelta(minutes=GAME_MIN.get(g.get("league"), 180))
        if g.get("state") == "in":
            end = max(end, now + timedelta(minutes=30))
        a, b = (g.get("teams") or [{}, {}])[:2]
        title = g.get("note") if g.get("combat") else f"{a.get('name') or a.get('abbr')} at {b.get('name') or b.get('abbr')}"
        for c in g.get("cable") or []:
            by.setdefault(int(c["ch"]), []).append({
                "start": start.isoformat(), "end": end.isoformat(), "title": title,
                "sub": g.get("detail") if g.get("state") == "in" else (g.get("series") or g.get("league")),
                "kind": "game", "league": g.get("league"), "live": g.get("state") == "in", "game_id": g.get("id"),
                "teams": [{"abbr": t.get("abbr"), "score": t.get("score"), "color": t.get("color"), "logo": t.get("logo")} for t in (a, b)],
                "close": g.get("close"), "fav": g.get("fav"), "apps": g.get("apps") or [], "estimated_end": True,
            })
    return by


def _adopt(listed: List[Dict[str, Any]], games: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A game the listings already carry keeps the listing's real start/end
    and gains the game's live details (teams, score, close). Games the
    listings don't have stay as their own (estimated-length) blocks."""
    rest = []
    for g in games:
        gs = _dt(g["start"])
        hit = next((p for p in listed if p.get("kind") in ("sports", "game") and abs((_dt(p["start"]) - gs).total_seconds()) <= 45 * 60), None)
        if hit:
            hit.update({k: g[k] for k in ("kind", "league", "live", "game_id", "teams", "close", "fav") if k in g})
            hit["apps"] = g.get("apps") or hit.get("apps") or []
            hit["sub"] = g.get("sub") if g.get("live") else (hit.get("sub") or g.get("sub"))
            hit["title"] = g["title"]
        else:
            rest.append(g)
    return rest


def _merge(shows: List[Dict[str, Any]], games: List[Dict[str, Any]], t0: datetime, t1: datetime) -> List[Dict[str, Any]]:
    """Games first; shows only where they don't collide with a game; no overlaps; clipped to the window."""
    def span(p):
        return _dt(p["start"]), _dt(p["end"])
    keep: List[Dict[str, Any]] = []
    for p in sorted(games, key=lambda p: p["start"]):
        s, e = span(p)
        if e > t0 and s < t1 and all(not (s < span(k)[1] and e > span(k)[0]) for k in keep):
            keep.append(p)
    for p in sorted(shows, key=lambda p: p["start"]):
        s, e = span(p)
        if e <= t0 or s >= t1:
            continue
        if any(s < span(k)[1] and e > span(k)[0] for k in keep):
            continue
        keep.append(p)
    return sorted(keep, key=lambda p: p["start"])


async def grid() -> Dict[str, Any]:
    hit = _cached("grid", 60)
    if hit is not None:
        return hit
    from services import watch
    now = datetime.now(timezone.utc)
    t0 = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    t1 = t0 + timedelta(hours=HOURS)
    et = now.astimezone(ET)
    days = sorted({et.date().isoformat(), (et + timedelta(hours=HOURS)).date().isoformat(), (et - timedelta(hours=4)).date().isoformat()})
    from services import epg
    async with httpx.AsyncClient(timeout=8.0, headers=UA) as client:
        eps_days, full = await asyncio.gather(asyncio.gather(*[_tvmaze_day(client, d) for d in days]), epg.listings())
    tonight = await watch.tonight()
    maze = _shows([e for day in eps_days for e in day])
    games = _games((tonight.get("live") or []) + (tonight.get("upcoming") or []), now)
    favs = set(htc.FAVORITES)
    rows = []
    for n, name, tier in htc.LINEUP:
        # Full Gracenote listings where the feed has the channel; TVmaze otherwise.
        listed = [dict(p) for p in full.get(n, [])] or maze.get(n, [])
        if listed and n in full:
            for p in listed:   # where that network's shows actually stream in his apps
                p["apps"] = p.get("apps") or (CHANNEL_APPS.get(n, []) if p.get("kind") != "sports" else [])
        extra = _adopt(listed, games.get(n, []))
        progs = _merge(listed, extra, t0, t1)
        rows.append({"ch": n, "name": name, "short": htc.SHORT.get(n, name), "cat": htc.CATEGORY.get(n, "Shows"),
                     "fav": n in favs, "programs": progs})
    return _store("grid", {"start": t0.isoformat(), "end": t1.isoformat(), "now": now.isoformat(), "channels": rows,
                           "credit": "Listings: EPGTalk (Gracenote) + TVmaze (CC BY-SA) · Games: ESPN · Lineup: HTC Horry/Marion"})
