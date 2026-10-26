"""Tonight, for the hub's Watch space: live sports + the evening's guide +
one smart pick, each item resolved to WHICH of the owner's apps can play it.

Sources (both keyless, both personal-use — same posture as the arcade's
sports.py, which reads the same ESPN feed):
  * ESPN site API scoreboards — games, scores, clocks, and per-game broadcast
    networks ("ESPN", "ABC", "Disney+", "Fox", "ESPN+"…). The broadcast list
    is what turns a scoreboard into a channel guide.
  * TVmaze schedules — broadcast primetime and streaming drops. TVmaze data
    is CC BY-SA: the UI must credit it.

Rules carried over from the arcade's sports engine:
  1. Never invent numbers. Win probability only when ESPN's summary has it
     (it never does for NHL); otherwise it's absent, not guessed.
  2. Never block: short timeouts, last-good cache, every source degrades on
     its own.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

ET = ZoneInfo("America/New_York")
ESPN = "https://site.web.api.espn.com/apis/site/v2/sports"
LEAGUES = {   # the arcade's default four + college football
    "NFL": "football/nfl",
    "MLB": "baseball/mlb",
    "NBA": "basketball/nba",
    "NHL": "hockey/nhl",
    "NCAAF": "football/college-football",
}

# Broadcast / network name -> the owner's app (keys match static/streaming-services.js).
# Matched case-insensitively against ESPN broadcast names and TVmaze networks.
WHERE = [
    (r"^disney\+$", "disney"),
    (r"^(espn|espn2|espnu|espnews|espn\+|espn deportes|abc|acc ?network|acc ?n|sec ?network|sec ?n)$", "espn"),
    (r"^(tnt|tbs|trutv|tnt sports|max|hbo max|hbo)$", "max"),
    (r"^(nbc|peacock|nbc\.com)$", "peacock"),
    (r"^(cbs|paramount\+|cbs sports network|cbssn)$", "paramount"),
    (r"^hulu$", "hulu"),
    (r"^netflix$", "netflix"),
    (r"^(apple tv\+?|apple tv|mls season pass)$", "appletv"),
    (r"^(youtube|youtube tv)$", "youtube"),
]
NATIONAL = re.compile(r"^(espn|espn2|abc|fox|nbc|cbs|tnt|tbs|truTV|prime video|netflix|peacock|nfl net|nfl network|fs1)$", re.I)

_cache: Dict[str, Dict[str, Any]] = {}


def _cached(key: str, ttl: float):
    hit = _cache.get(key)
    if hit and time.time() - hit["ts"] < ttl:
        return hit["data"]
    return None


def _store(key: str, data: Any) -> Any:
    _cache[key] = {"ts": time.time(), "data": data}
    return data


# ABC's SPORTS simulcast in the ESPN app; ABC's regular shows aren't there.
SPORTS_ONLY = {("abc", "espn")}


def apps_for(networks: List[str], sports: bool = True) -> List[str]:
    """Which of the owner's apps can play something on these networks (ordered, unique)."""
    out: List[str] = []
    for n in networks:
        name = (n or "").strip()
        for pat, app in WHERE:
            if not sports and (name.lower(), app) in SPORTS_ONLY:
                continue
            if re.match(pat, name, re.I) and app not in out:
                out.append(app)
    return out


# ── Sports ──────────────────────────────────────────────────────────────────

def _game(league: str, e: Dict[str, Any]) -> Dict[str, Any]:
    c = (e.get("competitions") or [{}])[0]
    st = e.get("status") or {}
    stype = st.get("type") or {}
    teams = []
    for x in sorted(c.get("competitors") or [], key=lambda x: x.get("homeAway") != "away"):   # away first
        t = x.get("team") or {}
        teams.append({
            "abbr": t.get("abbreviation"),
            "name": t.get("shortDisplayName") or t.get("name"),
            "color": t.get("color"),
            "alt": t.get("alternateColor"),
            "logo": t.get("logo"),
            "score": x.get("score") if stype.get("state") != "pre" else None,
            "record": next((r.get("summary") for r in x.get("records") or [] if r.get("summary")), None),
            "home": x.get("homeAway") == "home",
        })
    nets: List[str] = []
    for b in (c.get("broadcasts") or []):
        for n in b.get("names") or []:
            if n and n not in nets:
                nets.append(n)
    sit = c.get("situation") or {}
    situation = None
    if stype.get("state") == "in" and sit:
        situation = sit.get("downDistanceText") or sit.get("shortDownDistanceText") or (
            f"{sit.get('outs')} out" if sit.get("outs") is not None else None)
    note = next((n.get("headline") for n in c.get("notes") or [] if n.get("headline")), None)
    return {
        "id": e.get("id"),
        "league": league,
        "start": e.get("date"),
        "state": stype.get("state"),            # pre | in | post
        "detail": stype.get("shortDetail") or stype.get("detail"),
        "period": st.get("period"),
        "clock": st.get("displayClock"),
        "teams": teams,
        "networks": nets,
        "apps": apps_for(nets),
        "national": any(NATIONAL.match(n) for n in nets),
        "postseason": ((e.get("season") or {}).get("type") == 3),
        "note": note,
        "series": (c.get("series") or {}).get("summary"),
        "situation": situation,
        "win": None,
    }


async def _scoreboard(client: httpx.AsyncClient, league: str, ymd: str) -> List[Dict[str, Any]]:
    key = f"sb:{league}:{ymd}"
    hit = _cached(key, 45)
    if hit is not None:
        return hit
    try:
        r = await client.get(f"{ESPN}/{LEAGUES[league]}/scoreboard", params={"dates": ymd})
        r.raise_for_status()
        games = [_game(league, e) for e in (r.json() or {}).get("events") or []]
    except Exception as e:
        print(f"watch scoreboard {league} error: {e}")
        stale = _cache.get(key)
        return stale["data"] if stale else []
    return _store(key, games)


async def _win_prob(client: httpx.AsyncClient, g: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Home win % from ESPN's summary — only exists for live NFL/NBA/MLB/NCAAF.
    Never computed or guessed here."""
    if g["league"] == "NHL":
        return None
    key = f"wp:{g['id']}"
    hit = _cached(key, 60)
    if hit is not None:
        return hit or None
    try:
        r = await client.get(f"{ESPN}/{LEAGUES[g['league']]}/summary", params={"event": g["id"]})
        r.raise_for_status()
        wp = (r.json() or {}).get("winprobability") or []
        last = wp[-1] if wp else None
        val = {"home": round(float(last["homeWinPercentage"]) * 100)} if last and last.get("homeWinPercentage") is not None else {}
    except Exception:
        val = {}
    _store(key, val)
    return val or None


def _closeness(g: Dict[str, Any]) -> Optional[str]:
    """'Close · late' when a live game is tight in its final stretch."""
    if g["state"] != "in":
        return None
    try:
        a, b = (int(t["score"] or 0) for t in g["teams"][:2])
    except Exception:
        return None
    margin, p = abs(a - b), g.get("period") or 0
    late = {"NFL": p >= 4, "NCAAF": p >= 4, "NBA": p >= 4, "NHL": p >= 3, "MLB": p >= 7}.get(g["league"], False)
    tight = {"NFL": 8, "NCAAF": 8, "NBA": 6, "NHL": 1, "MLB": 2}.get(g["league"], 3)
    if late and margin <= tight:
        return "Close · late"
    if margin <= tight and p >= 2:
        return "Close"
    return None


# ── Guide (TVmaze) ─────────────────────────────────────────────────────────

BROADCAST_NETS = {"ABC", "CBS", "NBC", "FOX", "The CW", "PBS"}


async def _guide(client: httpx.AsyncClient, ymd_dash: str) -> List[Dict[str, Any]]:
    key = f"tvm:{ymd_dash}"
    hit = _cached(key, 1800)
    if hit is not None:
        return hit
    items: List[Dict[str, Any]] = []
    try:
        bc, web = await asyncio.gather(
            client.get("https://api.tvmaze.com/schedule", params={"country": "US", "date": ymd_dash}),
            client.get("https://api.tvmaze.com/schedule/web", params={"country": "US", "date": ymd_dash}),
        )
        for e in (bc.json() if bc.status_code == 200 else []) or []:
            show = e.get("show") or {}
            net = (show.get("network") or {}).get("name")
            at = e.get("airtime") or ""
            if net not in BROADCAST_NETS or not ("19:00" <= at <= "23:59"):
                continue
            items.append({"kind": "show", "time": at, "title": show.get("name"), "episode": e.get("name"),
                          "network": net, "apps": apps_for([net], sports=False), "genres": show.get("genres") or [],
                          "sports": "Sports" in (show.get("genres") or []) or show.get("type") == "Sports"})
        for e in (web.json() if web.status_code == 200 else []) or []:
            show = (e.get("_embedded") or {}).get("show") or {}
            ch = (show.get("webChannel") or {}).get("name")
            apps = apps_for([ch], sports=False) if ch else []
            if not apps or show.get("type") in ("News", "Talk Show"):
                continue   # only drops on services the owner actually has
            items.append({"kind": "stream", "time": None, "title": show.get("name"), "episode": e.get("name"),
                          "network": ch, "apps": apps, "genres": show.get("genres") or [], "sports": False})
    except Exception as e:
        print(f"watch guide error: {e}")
        stale = _cache.get(key)
        return stale["data"] if stale else []
    return _store(key, items)


# ── The pick ───────────────────────────────────────────────────────────────

def _game_score(g: Dict[str, Any], now: datetime) -> float:
    if g["state"] == "post":
        return -1
    s = 0.0
    if g["state"] == "in":
        s += 100 + (40 if g.get("close") == "Close · late" else 20 if g.get("close") else 0)
    else:
        try:
            start = datetime.fromisoformat(g["start"].replace("Z", "+00:00"))
            hrs = (start - now).total_seconds() / 3600
        except Exception:
            hrs = 6
        if hrs > 6:
            return 5
        s += 60 - max(0, hrs) * 8
    if g["postseason"]:
        s += 30
    if g["league"] == "NFL":
        s += 15
    if g["national"]:
        s += 10
    if g["apps"]:
        s += 12
    return s


def _reason(g: Dict[str, Any]) -> str:
    """One line on why this is the pick — never a repeat of the title."""
    a, b = g["teams"][0], g["teams"][1] if len(g["teams"]) > 1 else {"abbr": "?"}
    if g["state"] == "in":
        return f"{g.get('close') or 'Live'}: {a['abbr']} {a['score']}–{b.get('score')} {b['abbr']} · {g.get('detail') or ''}".strip(" ·")
    try:
        when = datetime.fromisoformat(g["start"].replace("Z", "+00:00")).astimezone(ET).strftime("%-I:%M %p")
    except Exception:
        when = ""
    bits = [f"{when} on {g['networks'][0]}" if when and g["networks"] else when,
            g.get("series"), "Postseason" if g["postseason"] and not g.get("note") else None]
    return " · ".join(x for x in bits if x)


async def tonight() -> Dict[str, Any]:
    now = datetime.now(ET)
    # The evening belongs to the day it started: after midnight, keep showing last night until 4 AM.
    day = (now - timedelta(hours=4)).date()
    ymd, ymd_dash = day.strftime("%Y%m%d"), day.isoformat()
    async with httpx.AsyncClient(timeout=6.0, headers={"User-Agent": "henderburgh-hub/1.0"}) as client:
        boards = await asyncio.gather(*[_scoreboard(client, lg, ymd) for lg in LEAGUES], _guide(client, ymd_dash))
        guide = boards.pop()
        games = [g for b in boards for g in b]
        live = [g for g in games if g["state"] == "in"]
        for g in live:
            g["close"] = _closeness(g)
        # Win probability for the four most watchable live games only.
        live.sort(key=lambda g: -_game_score(g, now))
        wps = await asyncio.gather(*[_win_prob(client, g) for g in live[:4]])
        for g, wp in zip(live[:4], wps):
            g["win"] = wp

    for g in games:
        g.setdefault("close", None)
        g["score"] = _game_score(g, now)

    # Games already in the sports list don't also belong in the TV guide
    # (TVmaze lists "World Series" on FOX as a show).
    guide = [i for i in guide if not i.get("sports")
             and not re.search(r"world series|monday night football|sunday night football|thursday night football|nfl|nba|mlb|nhl|college football|pregame|postgame", i.get("title") or "", re.I)]

    # NCAAF fills Saturdays with 60 games; outside the national/postseason
    # ones, only keep what's live — the guide is for tonight, not every game.
    games = [g for g in games if g["league"] != "NCAAF" or g["national"] or g["state"] == "in" or g["postseason"]]
    upcoming = sorted((g for g in games if g["state"] == "pre"), key=lambda g: g["start"] or "")
    finals = [g for g in games if g["state"] == "post"]

    # The pick: the best game, unless nothing sporty is on and a show leads.
    ranked = sorted((g for g in games if g["state"] != "post"), key=lambda g: -g["score"])
    pick = None
    if ranked and ranked[0]["score"] > 20:
        g = ranked[0]
        pick = {"kind": "game", "game_id": g["id"], "title": g.get("note") or f"{g['teams'][0]['name']} at {g['teams'][1]['name']}",
                "reason": _reason(g), "networks": g["networks"], "apps": g["apps"], "start": g["start"], "state": g["state"]}
    else:
        shows = [i for i in guide if i["kind"] == "show" and i["apps"]] or [i for i in guide if i["kind"] == "show"]
        if shows:
            s = sorted(shows, key=lambda i: i["time"] or "")[0]
            pick = {"kind": "show", "title": s["title"], "reason": f"{s['time']} on {s['network']}" + (f" · {s['episode']}" if s.get("episode") else ""),
                    "networks": [s["network"]], "apps": s["apps"], "start": None, "state": "pre"}

    return {
        "date": ymd_dash,
        "generated": now.isoformat(),
        "pick": pick,
        "live": sorted(live, key=lambda g: -g["score"]),
        "upcoming": upcoming,
        "finals": finals[-8:],
        "guide": guide,
        "credit": "Scores: ESPN · TV listings: TVmaze (CC BY-SA)",
    }
