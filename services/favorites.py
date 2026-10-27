"""The owner's teams and sports, for the hub's "Your teams" strip, the pick,
and kickoff reminders.

Owner's list (2026-10-26): Steelers, Pirates, Penguins, Pitt football;
NASCAR (no favourite driver); F1 with Verstappen; golf with Scheffler.

Sources: ESPN's site API (team schedules, racing + golf scoreboards and their
season calendars) and Jolpica's Ergast mirror for F1 standings / race times.
Same rules as services/watch.py: personal use, never invent a number, and
every source degrades on its own.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from services.watch import ESPN, ET, LEAGUES, _cached, _store, apps_for

# What he calls them (ESPN's short name for Pitt is "Panthers").
NICKNAMES = {("NCAAF", "221"): "Pitt"}

TEAMS = [   # (league, ESPN team id)
    ("NFL", "23"),     # Pittsburgh Steelers
    ("MLB", "23"),     # Pittsburgh Pirates
    ("NHL", "16"),     # Pittsburgh Penguins
    ("NCAAF", "221"),  # Pitt Panthers football
]
F1_DRIVER = "Verstappen"
GOLFER = "Scottie Scheffler"
UA = {"User-Agent": "henderburgh-hub/1.0"}


def is_favorite(g: Dict[str, Any]) -> bool:
    """A game from services/watch.py involving one of the owner's teams."""
    ids = {(lg, tid) for lg, tid in TEAMS}
    return any((g.get("league"), str(t.get("id"))) in ids for t in g.get("teams") or [])


def _nets(c: Dict[str, Any]) -> List[str]:
    out = []
    for b in c.get("broadcasts") or []:
        n = (b.get("media") or {}).get("shortName") or (b.get("names") or [None])[0]
        if n and n not in out:
            out.append(n)
    return out


def _score(x: Dict[str, Any]):
    s = x.get("score")
    return s.get("displayValue") if isinstance(s, dict) else s


async def _team(client: httpx.AsyncClient, league: str, tid: str) -> Optional[Dict[str, Any]]:
    try:
        r = await client.get(f"{ESPN}/{LEAGUES[league]}/teams/{tid}/schedule")
        r.raise_for_status()
        d = r.json() or {}
    except Exception as e:
        print(f"favorites team {league}/{tid} error: {e}")
        return None
    team = d.get("team") or {}
    out = {"league": league, "id": tid, "name": team.get("displayName"),
           "short": NICKNAMES.get((league, tid)) or team.get("name") or team.get("shortDisplayName"),
           "abbr": team.get("abbreviation"), "color": team.get("color"),
           "logo": ((team.get("logo") if isinstance(team.get("logo"), str) else None) or ((team.get("logos") or [{}])[0]).get("href")),
           "record": team.get("recordSummary"), "standing": team.get("standingSummary"),
           "last": None, "next": None, "season_over": False}
    events = d.get("events") or []
    for e in events:
        c = (e.get("competitions") or [{}])[0]
        state = ((c.get("status") or {}).get("type") or {}).get("state")
        me = next((x for x in c.get("competitors") or [] if str((x.get("team") or {}).get("id")) == tid), None)
        opp = next((x for x in c.get("competitors") or [] if str((x.get("team") or {}).get("id")) != tid), None)
        if not me or not opp:
            continue
        o = opp.get("team") or {}
        row = {"start": e.get("date"), "home": me.get("homeAway") == "home", "opponent": o.get("abbreviation"),
               "opponent_name": o.get("displayName"), "networks": _nets(c)}
        row["apps"] = apps_for(row["networks"])
        if state == "post":
            row.update(us=_score(me), them=_score(opp), won=bool(me.get("winner")))
            out["last"] = row
        elif state in ("pre", "in") and out["next"] is None:
            row["live"] = state == "in"
            if state == "in":
                row.update(us=_score(me), them=_score(opp))
            out["next"] = row
    if out["next"] is None:
        out["season_over"] = True
    return out


async def _calendar_next(client: httpx.AsyncClient, path: str) -> Optional[Dict[str, Any]]:
    """The live or next event in a racing/golf season, with its broadcasts."""
    try:
        r = await client.get(f"{ESPN}/{path}/scoreboard")
        r.raise_for_status()
        d = r.json() or {}
    except Exception:
        return None
    ev = (d.get("events") or [None])[0]
    if ev and ((ev.get("status") or {}).get("type") or {}).get("state") == "in":
        return {"event": ev, "live": True}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")   # same shape as ESPN's calendar dates
    cal = ((d.get("leagues") or [{}])[0].get("calendar")) or []
    nxt = next((c for c in cal if isinstance(c, dict) and (c.get("endDate") or c.get("startDate") or "") > now), None)
    if not nxt:
        return None
    # Pull that weekend's scoreboard for its broadcast networks.
    try:
        day = (nxt.get("startDate") or "")[:10].replace("-", "")
        r2 = await client.get(f"{ESPN}/{path}/scoreboard", params={"dates": day})
        ev2 = ((r2.json() or {}).get("events") or [None])[0] if r2.status_code == 200 else None
    except Exception:
        ev2 = None
    return {"label": nxt.get("label"), "start": nxt.get("startDate"), "event": ev2, "live": False}


async def _nascar(client) -> Optional[Dict[str, Any]]:
    n = await _calendar_next(client, "racing/nascar-premier")
    if not n:
        return None
    ev = n.get("event") or {}
    c = (ev.get("competitions") or [{}])[0]
    nets = [x for b in c.get("broadcasts") or [] for x in b.get("names") or []]
    return {"sport": "NASCAR", "title": ev.get("name") or n.get("label"), "start": c.get("date") or ev.get("date") or n.get("start"),
            "live": n["live"], "networks": nets, "apps": apps_for(nets)}


async def _f1(client) -> Optional[Dict[str, Any]]:
    out: Dict[str, Any] = {"sport": "F1", "driver": F1_DRIVER, "standing": None, "title": None, "start": None,
                           "networks": [], "apps": [], "live": False}
    try:
        st, nx = await asyncio.gather(client.get("https://api.jolpi.ca/ergast/f1/current/driverstandings.json"),
                                      client.get("https://api.jolpi.ca/ergast/f1/current/next.json"))
        lists = (st.json()["MRData"]["StandingsTable"]["StandingsLists"] or [{}])
        rows = lists[0].get("DriverStandings") or []
        me = next((s for s in rows if (s.get("Driver") or {}).get("familyName") == F1_DRIVER), None)
        lead = rows[0] if rows else None
        if me:
            out["standing"] = {"position": int(me["position"]), "points": float(me["points"]),
                               "leader": (lead or {}).get("Driver", {}).get("familyName"),
                               "behind": float(lead["points"]) - float(me["points"]) if lead else None,
                               "round": lists[0].get("round")}
        race = (nx.json()["MRData"]["RaceTable"]["Races"] or [None])[0]
        if race:
            out["title"] = race.get("raceName")
            out["start"] = f"{race['date']}T{race.get('time', '00:00:00Z')}"
    except Exception as e:
        print(f"favorites f1 error: {e}")
    n = await _calendar_next(client, "racing/f1")
    if n:
        ev = n.get("event") or {}
        c = (ev.get("competitions") or [{}])[0]
        out["networks"] = [x for b in c.get("broadcasts") or [] for x in b.get("names") or []]
        out["apps"] = apps_for(out["networks"])
        out["live"] = n["live"]
    return out


async def _golf(client) -> Optional[Dict[str, Any]]:
    n = await _calendar_next(client, "golf/pga")
    if not n:
        return None
    ev = n.get("event") or {}
    c = (ev.get("competitions") or [{}])[0]
    nets = [x for b in c.get("broadcasts") or [] for x in b.get("names") or []]
    # Every PGA Tour event streams featured groups on ESPN+ (PGA TOUR LIVE),
    # whatever TV network has the window — so golf always has a home in his apps.
    apps = apps_for(nets) or ["espn"]
    out = {"sport": "Golf", "title": ev.get("name") or n.get("label"), "start": ev.get("date") or n.get("start"),
           "live": n["live"], "networks": nets, "apps": apps, "via": None if apps_for(nets) else "PGA TOUR LIVE on ESPN+",
           "golfer": GOLFER, "golfer_pos": None, "leader": None}
    if n["live"]:
        comps = c.get("competitors") or []
        lead = next((x for x in comps if x.get("order") == 1), None)
        me = next((x for x in comps if (x.get("athlete") or {}).get("displayName") == GOLFER), None)
        if lead:
            out["leader"] = {"name": (lead.get("athlete") or {}).get("displayName"), "score": lead.get("score")}
        if me:
            out["golfer_pos"] = {"order": me.get("order"), "score": me.get("score")}
    return out


async def overview() -> Dict[str, Any]:
    hit = _cached("favorites", 300)
    if hit is not None:
        return hit
    from services import combat
    async with httpx.AsyncClient(timeout=6.0, headers=UA) as client:
        res = await asyncio.gather(*[_team(client, lg, tid) for lg, tid in TEAMS], _nascar(client), _f1(client), _golf(client),
                                   combat.next_up(client))
    teams = [t for t in res[:len(TEAMS)] if t]
    nascar, f1, golf, fights = res[len(TEAMS):]
    return _store("favorites", {"teams": teams, "nascar": nascar, "f1": f1, "golf": golf,
                                "ufc": fights.get("ufc"), "boxing": fights.get("boxing")})


def kickoff_soon(teams: List[Dict[str, Any]], now: datetime, within_min: int = 15) -> List[Dict[str, Any]]:
    """Favourite-team games starting in the next `within_min` minutes."""
    out = []
    for t in teams:
        n = t.get("next") or {}
        if not n or n.get("live") or not n.get("start"):
            continue
        try:
            start = datetime.fromisoformat(n["start"].replace("Z", "+00:00"))
        except Exception:
            continue
        mins = (start - now).total_seconds() / 60
        if 0 <= mins <= within_min:
            out.append({"team": t, "game": n, "minutes": round(mins)})
    return out
