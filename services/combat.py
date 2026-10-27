"""Fight nights, shaped like any other game so the hub treats them the same.

UFC (numbered events, Fight Nights and Dana White's Contender Series all
live in ESPN's UFC calendar) and the big boxing fights (TheSportsDB's
Boxing league, which only carries notable cards). Each event becomes one
"game": the main event's two fighters are the two sides, the event name is
the note, the main card's start is the start, and its broadcast resolves to
the owner's apps like everything else (UFC is on Paramount+).

Personal use, keyless (TheSportsDB's public test key). Never invents a
network: a boxing card with no listed broadcaster just has none.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from services.watch import ESPN, ET, _cached, _store, apps_for

UFC = "mma/ufc"
TSDB = "https://www.thesportsdb.com/api/v1/json/123"
BOXING_LEAGUE = "4445"


def _fighter(x: Dict[str, Any]) -> Dict[str, Any]:
    a = x.get("athlete") or {}
    full = a.get("displayName") or a.get("fullName") or "?"
    flag = (a.get("flag") or {}).get("href")
    return {"id": str(x.get("id") or ""), "abbr": full.split()[-1].upper(), "name": full, "color": None, "alt": None,
            "logo": flag if str(flag or "").startswith("https://") else None, "score": None,
            "record": next((r.get("summary") for r in x.get("records") or [] if r.get("summary")), None),
            "home": x.get("order") == 2, "winner": x.get("winner"), "periods": []}


def _ufc_event(e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    comps = e.get("competitions") or []
    if not comps:
        return None
    main = comps[-1]                      # ESPN lists the card in order; the main event is last
    fighters = [_fighter(x) for x in sorted(main.get("competitors") or [], key=lambda x: x.get("order") or 0)]
    if len(fighters) != 2:
        return None
    nets: List[str] = []
    for b in main.get("broadcasts") or []:
        for n in b.get("names") or []:
            if n and n not in nets:
                nets.append(n)
    st = (e.get("status") or {}).get("type") or {}
    mst = (main.get("status") or {}).get("type") or {}
    state = "in" if "in" in (st.get("state"), mst.get("state")) else st.get("state") or "pre"
    if mst.get("state") == "post":
        state = "post"
    name = e.get("name") or ""
    return {
        "id": str(e.get("id")), "league": "UFC", "start": main.get("date") or e.get("date"), "prelims": e.get("date"),
        "state": state, "detail": mst.get("shortDetail") if state != "pre" else None,
        "period": None, "clock": None, "teams": fighters, "networks": nets, "apps": apps_for(nets),
        "national": True, "postseason": False, "note": name,
        "series": (main.get("type") or {}).get("abbreviation"),      # weight class of the main event
        "situation": None, "last_play": None, "play": None, "win": None, "combat": True,
        "big": name.startswith("UFC ") and name[4:5].isdigit(),     # numbered pay-per-view cards
    }


async def ufc_day(client: httpx.AsyncClient, ymd: str) -> List[Dict[str, Any]]:
    key = f"ufc:{ymd}"
    hit = _cached(key, 60)
    if hit is not None:
        return hit
    try:
        r = await client.get(f"{ESPN}/{UFC}/scoreboard", params={"dates": ymd})
        r.raise_for_status()
        out = [g for g in (_ufc_event(e) for e in (r.json() or {}).get("events") or []) if g]
    except Exception as e:
        print(f"ufc scoreboard error: {e}")
        out = []
    return _store(key, out)


def _boxing_event(e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = e.get("strEvent") or ""
    if not e.get("dateEvent"):
        return None
    sides = [s.strip() for s in title.split(" vs ")] if " vs " in title else [title, ""]
    t = (e.get("strTime") or "00:00:00")[:8]
    start = f"{e['dateEvent']}T{t}Z" if e.get("strTimestamp") is None else (e["strTimestamp"].replace(" ", "T")[:19] + "Z")
    nets = [n.strip() for n in (e.get("strTVStation") or "").split(",") if n.strip()]
    status = (e.get("strStatus") or "").lower()
    state = "post" if status in ("ft", "finished", "match finished") or e.get("intHomeScore") is not None else "pre"
    side = lambda n, home: {"id": "", "abbr": (n.split()[-1] if n else "?").upper(), "name": n or "?", "color": None, "alt": None,   # noqa: E731
                            "logo": None, "score": None, "record": None, "home": home, "winner": None, "periods": []}
    return {
        "id": f"box{e.get('idEvent')}", "league": "BOXING", "start": start, "state": state, "detail": None,
        "period": None, "clock": None, "teams": [side(sides[0], False), side(sides[1] if len(sides) > 1 else "", True)],
        "networks": nets, "apps": apps_for(nets), "national": True, "postseason": False,
        "note": title, "series": e.get("strVenue"), "situation": None, "last_play": None, "play": None, "win": None,
        "combat": True, "big": True,
    }


async def boxing_next(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    hit = _cached("boxing:next", 3600)
    if hit is not None:
        return hit
    try:
        r = await client.get(f"{TSDB}/eventsnextleague.php", params={"id": BOXING_LEAGUE})
        r.raise_for_status()
        out = [g for g in (_boxing_event(e) for e in (r.json() or {}).get("events") or []) if g]
    except Exception as e:
        print(f"boxing schedule error: {e}")
        out = []
    return _store("boxing:next", out)


def _et_day(iso: Optional[str]):
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(ET)
        return (dt - timedelta(hours=4)).date()     # same 4 AM day boundary as tonight()
    except Exception:
        return None


async def tonight(client: httpx.AsyncClient, ymd: str, day) -> List[Dict[str, Any]]:
    """Fight cards whose main card falls on this evening, plus anything live."""
    ufc, box = await asyncio.gather(ufc_day(client, ymd), boxing_next(client))
    return [g for g in ufc + box if g["state"] == "in" or _et_day(g["start"]) == day]


async def next_up(client: httpx.AsyncClient) -> Dict[str, Optional[Dict[str, Any]]]:
    """The next UFC card (any kind, DWCS included) and the next big fight, for the follow strip."""
    out: Dict[str, Optional[Dict[str, Any]]] = {"ufc": None, "boxing": None}
    try:
        r = await client.get(f"{ESPN}/{UFC}/scoreboard")
        r.raise_for_status()
        d = r.json() or {}
        evs = [g for g in (_ufc_event(e) for e in d.get("events") or []) if g and g["state"] != "post"]
        if not evs:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            cal = ((d.get("leagues") or [{}])[0].get("calendar")) or []
            nxt = next((c for c in cal if isinstance(c, dict) and (c.get("startDate") or "") > now), None)
            if nxt:
                r2 = await client.get(f"{ESPN}/{UFC}/scoreboard", params={"dates": nxt["startDate"][:10].replace("-", "")})
                evs = [g for g in (_ufc_event(e) for e in (r2.json() or {}).get("events") or []) if g]
        out["ufc"] = evs[0] if evs else None
    except Exception as e:
        print(f"ufc next error: {e}")
    box = await boxing_next(client)
    out["boxing"] = box[0] if box else None
    return out
