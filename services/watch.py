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
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
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
    # The owner's services (2026-10-26): Netflix, Prime Video, Hulu, ESPN+,
    # HBO Max, Paramount+, Peacock, Disney+, Plex, Tubi.
    # ESPN+ is NOT the ESPN channel: linear ESPN/ESPN2/ABC games are only in
    # his apps when Disney+ is listed as a simulcast (e.g. Monday Night Football).
    (r"^disney\+$", "disney"),
    (r"^(espn\+|espn plus)$", "espn"),
    (r"^(tnt|tbs|trutv|tnt sports|max|hbo max|hbo)$", "max"),
    (r"^(nbc|peacock|nbc\.com)$", "peacock"),
    (r"^(cbs|paramount\+|cbs sports network|cbssn)$", "paramount"),
    (r"^hulu$", "hulu"),
    (r"^netflix$", "netflix"),
    (r"^(prime video|amazon prime video|amazon prime|prime)$", "prime"),
    (r"^(youtube)$", "youtube"),
    # free + legal (ad-supported)
    (r"^tubi$", "tubi"),
    (r"^pluto ?tv$", "pluto"),
    (r"^the roku channel$", "roku"),
    (r"^plex$", "plex"),
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


# Pairs that only hold for sports broadcasts (none today; kept for the rule).
SPORTS_ONLY: set = set()


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
            "id": t.get("id"),
            "abbr": t.get("abbreviation"),
            "name": t.get("shortDisplayName") or t.get("name"),
            "color": t.get("color"),
            "alt": t.get("alternateColor"),
            "logo": t.get("logo"),
            "score": x.get("score") if stype.get("state") != "pre" else None,
            "record": next((r.get("summary") for r in x.get("records") or [] if r.get("summary")), None),
            "home": x.get("homeAway") == "home",
            "periods": [ls.get("displayValue") or (str(int(ls["value"])) if isinstance(ls.get("value"), (int, float)) else None)
                        for ls in x.get("linescores") or []] if stype.get("state") != "pre" else [],
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
    lp = (sit.get("lastPlay") or {}) if stype.get("state") == "in" else {}
    last_play = (lp.get("text") or "").strip()
    # Who made the last play (for the scoring moment's portrait), and whether it scored.
    who = next(iter(lp.get("athletesInvolved") or []), {}) if lp else {}
    play = {"kind": (lp.get("type") or {}).get("text"), "scored": bool(lp.get("scoreValue")),
            "team": str((lp.get("team") or {}).get("id") or ""),
            "who": who.get("displayName"),
            "headshot": who.get("headshot") if str(who.get("headshot") or "").startswith("https://") else None} if lp else None
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
        "last_play": last_play or None,     # ESPN's own play-by-play line, verbatim
        "play": play,
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
    if g["state"] != "in" or g.get("combat"):
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
    if g["postseason"] or g.get("big"):     # playoffs, numbered UFC cards, big fights
        s += 30
    if g["league"] == "NFL":
        s += 15
    if g["national"]:
        s += 10
    if g["apps"]:
        s += 12
    if g.get("fav"):
        s += 60          # his teams outrank everything but a close live game
    return s


def _ampm(t: Optional[str]) -> str:
    """TVmaze's '20:00' -> '8:00 PM'."""
    try:
        h, m = (int(x) for x in str(t).split(":")[:2])
        return f"{(h + 11) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"
    except Exception:
        return t or ""


def _reason(g: Dict[str, Any]) -> str:
    """One line on why this is the pick — never a repeat of the title."""
    a, b = g["teams"][0], g["teams"][1] if len(g["teams"]) > 1 else {"abbr": "?"}
    if g.get("combat"):
        main = f"Main event: {a['name']} vs {b.get('name')}" + (f" ({g['series']})" if g.get("series") else "")
        if g["state"] == "in":
            return f"Live · {main}"
        try:
            when = datetime.fromisoformat(g["start"].replace("Z", "+00:00")).astimezone(ET).strftime("%-I:%M %p")
        except Exception:
            when = ""
        return " · ".join(x for x in (f"Main card {when}" if when else None, main, f"on {g['networks'][0]}" if g["networks"] else None) if x)
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
        from services import combat   # local import: combat imports this module
        boards = await asyncio.gather(*[_scoreboard(client, lg, ymd) for lg in LEAGUES], combat.tonight(client, ymd, day),
                                      _guide(client, ymd_dash))
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

    from services import favorites, htc   # local import: favorites imports this module
    for g in games:
        g["cable"] = htc.channels_for(g.get("networks") or [])   # HTC channel numbers, when it's on cable
        g.setdefault("close", None)
        g["fav"] = favorites.is_favorite(g)
        g["score"] = _game_score(g, now)

    # Games already in the sports list don't also belong in the TV guide
    # (TVmaze lists "World Series" on FOX as a show).
    guide = [i for i in guide if not i.get("sports")
             and not re.search(r"world series|monday night football|sunday night football|thursday night football|nfl|nba|mlb|nhl|college football|pregame|postgame", i.get("title") or "", re.I)]

    # NCAAF fills Saturdays with 60 games; outside the national/postseason
    # ones, only keep what's live — the guide is for tonight, not every game.
    games = [g for g in games if g["league"] != "NCAAF" or g["national"] or g["state"] == "in" or g["postseason"] or g["fav"]]
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
            pick = {"kind": "show", "title": s["title"], "reason": f"{_ampm(s['time'])} on {s['network']}" + (f" · {s['episode']}" if s.get("episode") else ""),
                    "networks": [s["network"]], "apps": s["apps"], "start": None, "state": "pre"}

    for i in guide:
        i["cable"] = htc.channels_for([i.get("network")] if i.get("network") else [])
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


# ── Close-game alerts ───────────────────────────────────────────────────────
# A watchable game that turns "Close · late" earns one push, with buttons that
# put it on a TV straight from the lock screen. Each button is a signed,
# single-purpose link (this game, this TV, this app, two hours) so a leaked
# notification can't become a remote for the house.

# TV launch links — must match `tv` in static/streaming-services.js (app links,
# never packages: the TVs ignore bare package names).
APP_LINKS = {
    "netflix": ("Netflix", "https://www.netflix.com/title"),
    "max": ("HBO Max", "https://play.max.com"),
    "disney": ("Disney+", "https://www.disneyplus.com"),
    "hulu": ("Hulu", "https://www.hulu.com"),
    "espn": ("ESPN+", "https://www.espn.com/watch/"),
    "peacock": ("Peacock", "https://www.peacocktv.com"),
    "paramount": ("Paramount+", "https://www.paramountplus.com"),
    "youtube": ("YouTube", "https://www.youtube.com"),
    "prime": ("Prime Video", "https://app.primevideo.com"),
    "tubi": ("Tubi", "https://tubitv.com"),
    "pluto": ("Pluto TV", "https://pluto.tv"),
    "roku": ("The Roku Channel", "https://therokuchannel.roku.com"),
    "plex": ("Plex", "https://watch.plex.tv"),
}
OTA = re.compile(r"^(fox|nbc|cbs|abc|the cw|pbs)$", re.I)
ALERT_TTL = 2 * 3600
SENT_PATH = Path(os.getenv("DATA_DIR", "data").strip() or "data") / "close_alerts.json"


def watchable(g: Dict[str, Any]) -> bool:
    """Same test the Watch guide uses: his team, national, postseason, in his apps, or over the air."""
    return bool(g.get("fav") or g.get("national") or g.get("postseason") or g.get("apps") or any(OTA.match(n) for n in g.get("networks") or []))


def _secret() -> bytes:
    return (os.getenv("ADMIN_TOKEN", "") or "").encode() or b""


def sign_put(game_id: str, tv: str, app: str, now: Optional[float] = None) -> Optional[str]:
    """A link token for one launch. None when there's no secret to sign with."""
    key = _secret()
    if not key:
        return None
    exp = int((now or time.time()) + ALERT_TTL)
    body = f"{game_id}|{tv}|{app}|{exp}"
    mac = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}|{mac}"


def verify_put(token: str, now: Optional[float] = None) -> Optional[Dict[str, str]]:
    key = _secret()
    try:
        game_id, tv, app, exp, mac = (token or "").split("|")
    except ValueError:
        return None
    if not key:
        return None
    good = hmac.new(key, f"{game_id}|{tv}|{app}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(good, mac) or int(exp) < int(now or time.time()):
        return None
    if not re.fullmatch(r"remote\.[a-z0-9_]+", tv) or app not in APP_LINKS:
        return None
    return {"game_id": game_id, "tv": tv, "app": app}


def _load_sent() -> Dict[str, float]:
    try:
        data = json.loads(SENT_PATH.read_text())
        cutoff = time.time() - 2 * 86400
        return {k: v for k, v in data.items() if v > cutoff}
    except Exception:
        return {}


def _save_sent(sent: Dict[str, float]) -> None:
    try:
        SENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SENT_PATH.write_text(json.dumps(sent))
    except Exception as e:
        print(f"close alerts save error: {e}")


def alert_hours(now: datetime) -> bool:
    """9 AM through 1:30 AM ET — late enough for West Coast finishes, never the middle of the night."""
    m = now.hour * 60 + now.minute
    return m >= 9 * 60 or m <= 90


def build_alert(g: Dict[str, Any], tvs: List[Dict[str, Any]], base_url: str) -> Dict[str, Any]:
    """Title, body and buttons for one close game. Pure — testable without a phone."""
    a, b = g["teams"][0], g["teams"][1]
    net = (g.get("networks") or [""])[0]
    title = f"Close & late: {a['abbr']} {a['score']}-{b['score']} {b['abbr']}"
    app = next((k for k in g.get("apps") or [] if k in APP_LINKS), None)
    where = f"in {APP_LINKS[app][0]}" if app else (f"on {net} (live channels)" if net else "")
    body = " · ".join(x for x in [g.get("detail"), g.get("situation"), where] if x)
    actions = []
    if app:
        # The TV that's already on first — that's the one he's watching.
        for tv in sorted([t for t in tvs if t.get("available")], key=lambda t: not t.get("on"))[:2]:
            tok = sign_put(g["id"], tv["entity_id"], app)
            if tok:
                actions.append({"action": "http", "label": f"{APP_LINKS[app][0]} on {tv['name']}",
                                "url": f"{base_url}/api/watch/put?t={tok}", "method": "POST", "clear": True})
    actions.append({"action": "view", "label": "Open Watch", "url": f"{base_url}/hub#watch"})
    return {"title": title, "body": body, "actions": actions, "click": f"{base_url}/hub#watch"}


async def check_close_games(tvs: List[Dict[str, Any]], notify, base_url: str) -> List[str]:
    """One pass: push for every newly close-and-late watchable game. Returns game ids alerted."""
    now = datetime.now(ET)
    if not alert_hours(now):
        return []
    data = await tonight()
    sent = _load_sent()
    fired = []
    for g in data.get("live") or []:
        if g.get("close") != "Close · late" or not watchable(g) or g["id"] in sent:
            continue
        al = build_alert(g, tvs, base_url)
        ok = await asyncio.to_thread(notify, al["title"], al["body"], "high", ["rotating_light"], al["click"], al["actions"])
        if ok:
            sent[g["id"]] = time.time()
            fired.append(g["id"])
    # Kickoff reminders for his teams: one push ~15 minutes before.
    try:
        from services import favorites
        fav = await favorites.overview()
        for k in favorites.kickoff_soon(fav.get("teams") or [], datetime.now(ET).astimezone(), 15):
            t, n = k["team"], k["game"]
            key = f"kick:{t['league']}:{n['start']}"
            if key in sent:
                continue
            app = next((a for a in n.get("apps") or [] if a in APP_LINKS), None)
            where = f"in {APP_LINKS[app][0]}" if app else (f"on {n['networks'][0]}" if n.get("networks") else "")
            title = f"{t['short'] or t['name']} {'vs' if n['home'] else '@'} {n['opponent']} in {k['minutes']} min"
            body = " · ".join(x for x in [t.get("record"), where] if x)
            actions = []
            if app:
                for tv in sorted([x for x in tvs if x.get("available")], key=lambda x: not x.get("on"))[:2]:
                    tok = sign_put(key, tv["entity_id"], app)
                    if tok:
                        actions.append({"action": "http", "label": f"{APP_LINKS[app][0]} on {tv['name']}",
                                        "url": f"{base_url}/api/watch/put?t={tok}", "method": "POST", "clear": True})
            actions.append({"action": "view", "label": "Open Watch", "url": f"{base_url}/hub#watch"})
            ok = await asyncio.to_thread(notify, title, body, "high", ["football"], f"{base_url}/hub#watch", actions)
            if ok:
                sent[key] = time.time()
                fired.append(key)
    except Exception as e:
        print(f"kickoff reminder error: {e}")
    if fired:
        _save_sent(sent)
    return fired


# ── Find anything ──────────────────────────────────────────────────────────
# "Where can I watch X?" for shows (TVmaze) and teams (ESPN), answered in
# terms of HIS apps, with the next airing / next game. Movies aren't in
# either keyless source — say so rather than guess.

TEAM_LEAGUES = ["NFL", "NBA", "MLB", "NHL"]
NEXT_DAY = {"abc": "hulu", "fox": "hulu"}


async def _team_index(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    hit = _cached("teams", 86400)
    if hit is not None:
        return hit
    out: List[Dict[str, Any]] = []
    for lg in TEAM_LEAGUES:
        try:
            r = await client.get(f"{ESPN}/{LEAGUES[lg]}/teams")
            r.raise_for_status()
            for t in ((r.json().get("sports") or [{}])[0].get("leagues") or [{}])[0].get("teams") or []:
                tm = t.get("team") or {}
                out.append({"league": lg, "id": tm.get("id"), "name": tm.get("displayName"), "short": tm.get("shortDisplayName"),
                            "nick": tm.get("name"), "loc": tm.get("location"), "abbr": tm.get("abbreviation"),
                            "color": tm.get("color"), "logo": ((tm.get("logos") or [{}])[0]).get("href")})
        except Exception as e:
            print(f"team index {lg} error: {e}")
    return _store("teams", out) if out else out


def _match_teams(index: List[Dict[str, Any]], q: str) -> List[Dict[str, Any]]:
    ql = q.lower().strip()
    scored = []
    for t in index:
        names = [x.lower() for x in (t["name"], t["short"], t["nick"], t["loc"]) if x]
        if (t["abbr"] or "").lower() == ql:
            s = 3
        elif any(n == ql for n in names):
            s = 3
        elif any(n.startswith(ql) for n in names) and len(ql) >= 3:
            s = 2
        elif len(ql) >= 4 and any(ql in n for n in names):
            s = 1
        else:
            continue
        scored.append((s, t))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:3]]


async def _team_next(client: httpx.AsyncClient, t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        r = await client.get(f"{ESPN}/{LEAGUES[t['league']]}/teams/{t['id']}/schedule")
        r.raise_for_status()
        events = r.json().get("events") or []
    except Exception:
        return None
    for e in events:
        c = (e.get("competitions") or [{}])[0]
        state = ((c.get("status") or {}).get("type") or {}).get("state")
        if state not in ("pre", "in"):
            continue
        nets = [((b.get("media") or {}).get("shortName")) for b in c.get("broadcasts") or []]
        nets = [n for n in nets if n]
        opp = next(((x.get("team") or {}) for x in c.get("competitors") or [] if (x.get("team") or {}).get("id") != t["id"]), {})
        home = next((x.get("homeAway") == "home" for x in c.get("competitors") or [] if (x.get("team") or {}).get("id") == t["id"]), False)
        return {"name": e.get("name"), "start": e.get("date"), "state": state, "networks": nets, "apps": apps_for(nets),
                "opponent": opp.get("abbreviation") or opp.get("displayName"), "home": home}
    return None


async def find(q: str) -> Dict[str, Any]:
    q = (q or "").strip()[:60]
    if len(q) < 2:
        return {"q": q, "teams": [], "shows": []}
    key = f"find:{q.lower()}"
    hit = _cached(key, 300)
    if hit is not None:
        return hit
    async with httpx.AsyncClient(timeout=6.0, headers={"User-Agent": "henderburgh-hub/1.0"}) as client:
        index_task = asyncio.create_task(_team_index(client))
        try:
            r = await client.get("https://api.tvmaze.com/search/shows", params={"q": q})
            raw = r.json() if r.status_code == 200 else []
        except Exception:
            raw = []
        shows = []
        for x in raw[:4]:
            sh = x.get("show") or {}
            net = (sh.get("webChannel") or {}).get("name") or (sh.get("network") or {}).get("name")
            apps = apps_for([net], sports=False) if net else []
            later = None
            # Catalog answer (not tonight's live guide): ABC and FOX shows land
            # on Hulu the day after they air.
            if not apps and net and NEXT_DAY.get(net.lower()):
                apps, later = [NEXT_DAY[net.lower()]], "next day"
            # A network says where a show airs, not where an old, finished one
            # streams today — don't promise Paramount+ has a 1952 CBS show.
            yr = int((sh.get("premiered") or "0")[:4] or 0)
            if sh.get("status") == "Ended" and yr and yr < 2010 and sh.get("network") and not sh.get("webChannel"):
                apps, later = [], "older show — streaming home unknown"
            shows.append({"id": sh.get("id"), "title": sh.get("name"), "network": net, "apps": apps, "later": later,
                          "status": sh.get("status"), "year": (sh.get("premiered") or "")[:4] or None,
                          "image": ((sh.get("image") or {}).get("medium")), "genres": sh.get("genres") or [],
                          "next": None})
        # Next episode for running shows near the top.
        async def nxt(s):
            if s["status"] != "Running":
                return
            try:
                rr = await client.get(f"https://api.tvmaze.com/shows/{s['id']}", params={"embed": "nextepisode"})
                ep = ((rr.json() or {}).get("_embedded") or {}).get("nextepisode") if rr.status_code == 200 else None
                if ep:
                    s["next"] = {"airstamp": ep.get("airstamp"), "name": ep.get("name"), "season": ep.get("season"), "number": ep.get("number")}
            except Exception:
                pass
        await asyncio.gather(*[nxt(s) for s in shows[:2]])
        teams = _match_teams(await index_task, q)
        nexts = await asyncio.gather(*[_team_next(client, t) for t in teams])
        for t, n in zip(teams, nexts):

            t["next"] = n
    return _store(key, {"q": q, "teams": teams, "shows": shows,
                        "note": "Shows: TVmaze · Teams: ESPN · Movies aren't covered by these sources yet"})


# ── New on your apps ──────────────────────────────────────────────────────
# Season premieres and brand-new shows landing this week on services the owner
# actually has — the "a new season is out" moment, without checking each app.

async def new_this_week(days: int = 7) -> Dict[str, Any]:
    hit = _cached("new_week", 6 * 3600)
    if hit is not None:
        return hit
    start = (datetime.now(ET) - timedelta(hours=4)).date()
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "henderburgh-hub/1.0"}) as client:
        async def day(d):
            try:
                r = await client.get("https://api.tvmaze.com/schedule/web", params={"date": d})
                return d, (r.json() if r.status_code == 200 else [])
            except Exception:
                return d, []
        results = await asyncio.gather(*[day(d) for d in dates])
    seen, items = set(), []
    for d, eps in results:
        for e in eps or []:
            sh = (e.get("_embedded") or {}).get("show") or {}
            ch = (sh.get("webChannel") or {}).get("name")
            apps = apps_for([ch], sports=False) if ch else []
            if not apps or e.get("number") != 1 or sh.get("language") not in ("English", None):
                continue
            if sh.get("type") in ("News", "Talk Show", "Sports") or sh.get("id") in seen:
                continue
            seen.add(sh.get("id"))
            new_show = (sh.get("premiered") or "") == (e.get("airdate") or d)
            items.append({
                "title": sh.get("name"), "date": e.get("airdate") or d, "app": apps[0], "network": ch,
                "season": e.get("season"), "kind": "New show" if new_show else f"Season {e.get('season')}",
                "image": (sh.get("image") or {}).get("medium"), "genres": (sh.get("genres") or [])[:2],
                "rating": (sh.get("rating") or {}).get("average"),
            })
    # Returning seasons of rated shows first within a day, then new shows.
    items.sort(key=lambda i: (i["date"], i["kind"] == "New show", -(i["rating"] or 0)))
    return _store("new_week", {"from": dates[0], "items": items[:24]})
