"""Game Center: everything about one game, for the hub's game sheet.

One ESPN summary call (site API, keyless, personal use) normalised into a
sport-agnostic shape: both teams with period-by-period scores, a curated set
of head-to-head stats with comparison bars, each team's leaders, the scoring
summary, ESPN's win-probability curve when it publishes one, and the venue.

Same rules as services/watch.py: never invent a number (a stat ESPN doesn't
send is simply absent), cache briefly, and degrade to None on any failure.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from services.watch import ESPN, LEAGUES, _cached, _store

UA = {"User-Agent": "henderburgh-hub/1.0"}

# (label to show, candidate ESPN names/labels, lower_is_better)
KEY_STATS: Dict[str, List[Tuple[str, List[str], bool]]] = {
    "NHL": [("Shots", ["Shots", "shotsTotal"], False),
            ("Power play", ["__pp"], False),
            ("Faceoffs", ["Faceoff Win Percent", "faceoffPercent"], False),
            ("Hits", ["Hits", "hits"], False),
            ("Blocked", ["Blocked Shots", "blockedShots"], False),
            ("Takeaways", ["Takeaways", "takeaways"], False),
            ("Giveaways", ["Giveaways", "giveaways"], True),
            ("Penalty min", ["Penalty Minutes", "penaltyMinutes"], True)],
    "NBA": [("Field goals", ["FG"], False), ("3-pointers", ["3PT"], False), ("Free throws", ["FT"], False),
            ("Rebounds", ["Rebounds"], False), ("Assists", ["Assists"], False),
            ("Turnovers", ["Total Turnovers", "Turnovers"], True), ("In the paint", ["Points in Paint"], False),
            ("Fast break", ["Fast Break Points"], False), ("Largest lead", ["Largest Lead"], False)],
    "FOOTBALL": [("Total yards", ["Total Yards"], False), ("Passing", ["Passing"], False), ("Rushing", ["Rushing"], False),
                 ("1st downs", ["1st Downs"], False), ("3rd down", ["3rd down efficiency"], False),
                 ("Red zone", ["Red Zone (Made-Att)"], False), ("Turnovers", ["Turnovers"], True),
                 ("Penalties", ["Penalties"], True), ("Possession", ["Possession"], False)],
    "MLB": [("Hits", ["hits"], False), ("Home runs", ["homeRuns"], False), ("RBI", ["RBIs"], False),
            ("Walks", ["walks"], False), ("Strikeouts", ["strikeouts"], True), ("Left on base", ["runnersLeftOnBase", "LOB"], True)],
}
STAT_SET = {"NFL": "FOOTBALL", "NCAAF": "FOOTBALL"}


def _num(v: Optional[str]) -> Optional[float]:
    """A number to draw a bar from: '41-85' / '1/4' -> made, '12:31' -> seconds, '48.2' -> 48.2."""
    if v is None:
        return None
    s = str(v).strip()
    m = re.fullmatch(r"(\d+):(\d{2})", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.match(r"-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def _flat_stats(team: Dict[str, Any]) -> Dict[str, str]:
    """name/label -> displayValue, flattening MLB's batting/pitching groups."""
    out: Dict[str, str] = {}

    def add(s: Dict[str, Any]):
        dv = s.get("displayValue")
        if dv is None:
            return
        for k in (s.get("name"), s.get("label"), s.get("abbreviation")):
            if k and k not in out:
                out[k] = str(dv)

    for s in team.get("statistics") or []:
        if isinstance(s.get("stats"), list):
            for sub in s["stats"]:
                add(sub)
        else:
            add(s)
    return out


# Made-of-attempted stats compare as a rate, not as the made count.
RATE = {"Power play", "3rd down", "Red zone", "Field goals", "3-pointers", "Free throws"}


def _rate(v: Optional[str]) -> Optional[float]:
    m = re.fullmatch(r"\s*(\d+)\s*[-/]\s*(\d+)\s*", str(v or ""))
    if not m:
        return _num(v)
    made, att = int(m.group(1)), int(m.group(2))
    return round(made / att * 100, 1) if att else 0.0


def _pair(label: str, a: Optional[str], h: Optional[str], lower_better: bool) -> Optional[Dict[str, Any]]:
    if a is None and h is None:
        return None
    conv = _rate if label in RATE else _num
    na, nh = conv(a), conv(h)
    edge = None
    if na is not None and nh is not None and na != nh:
        edge = ("away" if na < nh else "home") if lower_better else ("away" if na > nh else "home")
    return {"label": label, "away": a, "home": h, "a": na, "h": nh, "edge": edge}


def _key_stats(league: str, away: Dict[str, str], home: Dict[str, str]) -> List[Dict[str, Any]]:
    rows = []
    for label, names, lower in KEY_STATS.get(STAT_SET.get(league, league), []):
        if names == ["__pp"]:     # NHL power play as goals/opportunities
            def pp(t):
                g, o = t.get("Power Play Goals"), t.get("Power Play Opportunities")
                return f"{g}/{o}" if g is not None and o is not None else None
            p = _pair(label, pp(away), pp(home), False)
        else:
            pick = lambda t: next((t[n] for n in names if n in t), None)   # noqa: E731
            p = _pair(label, pick(away), pick(home), lower)
        if p:
            rows.append(p)
    return rows


def _leaders(d: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for block in d.get("leaders") or []:
        tid = str((block.get("team") or {}).get("id"))
        rows = []
        for cat in (block.get("leaders") or [])[:3]:
            top = (cat.get("leaders") or [None])[0]
            if not top:
                continue
            a = top.get("athlete") or {}
            head = (a.get("headshot") or {}).get("href") if isinstance(a.get("headshot"), dict) else a.get("headshot")
            rows.append({"cat": cat.get("displayName") or cat.get("name"), "name": a.get("shortName") or a.get("displayName"),
                         "value": top.get("displayValue"), "headshot": head if head and str(head).startswith("https://") else None})
        out[tid] = rows
    return out


def _scoring(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    plays = d.get("scoringPlays") or [p for p in (d.get("plays") or []) if p.get("scoringPlay")]
    out = []
    for p in plays[-8:]:
        out.append({"period": (p.get("period") or {}).get("number"), "clock": (p.get("clock") or {}).get("displayValue"),
                    "text": (p.get("text") or "").strip(), "team": str((p.get("team") or {}).get("id") or ""),
                    "away": p.get("awayScore"), "home": p.get("homeScore"),
                    "kind": (p.get("scoringType") or {}).get("displayName") or (p.get("type") or {}).get("text")})
    return out


def _winprob(d: Dict[str, Any]) -> Optional[List[float]]:
    w = d.get("winprobability") or []
    if len(w) < 4:
        return None
    step = max(1, len(w) // 120)
    pts = [round(float(x.get("homeWinPercentage", 0.5)), 3) for x in w[::step]]
    last = round(float(w[-1].get("homeWinPercentage", 0.5)), 3)
    if pts[-1] != last:
        pts.append(last)
    return pts


async def game(league: str, event_id: str) -> Optional[Dict[str, Any]]:
    if league not in LEAGUES or not re.fullmatch(r"\d{4,14}", event_id or ""):
        return None
    key = f"gc:{league}:{event_id}"
    hit = _cached(key, 20)
    if hit is not None:
        return hit
    try:
        async with httpx.AsyncClient(timeout=6.0, headers=UA) as client:
            r = await client.get(f"{ESPN}/{LEAGUES[league]}/summary", params={"event": event_id})
            r.raise_for_status()
            d = r.json() or {}
    except Exception as e:
        print(f"gamecenter {league}/{event_id} error: {e}")
        return None

    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    status = (comp.get("status") or {}).get("type") or {}
    teams = []
    for x in sorted(comp.get("competitors") or [], key=lambda x: x.get("homeAway") != "away"):
        t = x.get("team") or {}
        logo = t.get("logo") or ((t.get("logos") or [{}])[0]).get("href")
        teams.append({
            "id": str(t.get("id")), "abbr": t.get("abbreviation"), "name": t.get("displayName"),
            "short": t.get("shortDisplayName") or t.get("name"), "location": t.get("location"),
            "color": t.get("color"), "alt": t.get("alternateColor"), "logo": logo,
            "score": x.get("score"), "home": x.get("homeAway") == "home", "winner": x.get("winner"),
            "record": next((r.get("summary") or r.get("displayValue") for r in x.get("record") or x.get("records") or [] if (r.get("summary") or r.get("displayValue"))), None),
            "periods": [ls.get("displayValue") for ls in x.get("linescores") or []],
        })
    box = {str((t.get("team") or {}).get("id")): _flat_stats(t) for t in (d.get("boxscore") or {}).get("teams") or []}
    away_id, home_id = (teams[0]["id"], teams[1]["id"]) if len(teams) == 2 else ("", "")
    info = d.get("gameInfo") or {}
    venue = (info.get("venue") or {})
    city = ((venue.get("address") or {}).get("city"))
    out = {
        "league": league, "id": event_id,
        "state": status.get("state"), "detail": status.get("shortDetail") or status.get("detail"),
        "teams": teams,
        "stats": _key_stats(league, box.get(away_id, {}), box.get(home_id, {})),
        "leaders": _leaders(d),
        "scoring": _scoring(d),
        "winprob": _winprob(d),       # home win %, oldest -> newest
        "venue": ", ".join(x for x in (venue.get("fullName"), city) if x) or None,
        "attendance": info.get("attendance"),
        "series": next((s.get("summary") for s in d.get("seasonseries") or [] if s.get("summary")), None),
    }
    return _store(key, out)
