"""Ambient feeds for the iPad home and room dashboards: calendar + music.

Calendar comes from Home Assistant's calendar.* entities (HA expands recurring
events, so there's no ICS parsing here). Add any calendar to HA — Google,
CalDAV/iCloud, or "Remote Calendar" with an ICS link — and it shows up.

Music comes from Last.fm (user.getRecentTracks) once LASTFM_USER and
LASTFM_API_KEY are set. Both feeds return None/[] when unconfigured so the
tiles can say exactly what's missing instead of faking data.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from config import HA_ENABLED, HA_HEADERS, HA_URL

LASTFM_USER = os.getenv("LASTFM_USER", "").strip()
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "").strip()

_cal_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_music_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
CAL_TTL = 300
MUSIC_TTL = 30


async def upcoming_events(limit: int = 3, days: int = 7) -> Dict[str, Any]:
    """Next `limit` events across every HA calendar in the coming `days`."""
    now_ts = time.time()
    if _cal_cache["data"] is not None and now_ts - _cal_cache["ts"] < CAL_TTL:
        return _cal_cache["data"]
    out: Dict[str, Any] = {"configured": False, "events": []}
    if not HA_ENABLED:
        return out
    now = datetime.now(timezone.utc)
    start, end = now.isoformat(), (now + timedelta(days=days)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"{HA_URL}/api/calendars", headers=HA_HEADERS)
            # 404 = HA has no calendar integration loaded yet: a normal "not set up" state.
            cals = (r.json() or []) if r.status_code == 200 else []
            out["configured"] = bool(cals)
            events: List[Dict[str, Any]] = []
            for cal in cals:
                r = await client.get(f"{HA_URL}/api/calendars/{cal['entity_id']}",
                                     params={"start": start, "end": end}, headers=HA_HEADERS)
                if r.status_code != 200:
                    continue
                for ev in r.json() or []:
                    s, e = ev.get("start") or {}, ev.get("end") or {}
                    events.append({
                        "title": ev.get("summary") or "(no title)",
                        "start": s.get("dateTime") or s.get("date"),
                        "end": e.get("dateTime") or e.get("date"),
                        "all_day": "date" in s and "dateTime" not in s,
                        "location": ev.get("location"),
                        "calendar": cal.get("name"),
                    })
    except Exception as e:
        print(f"calendar fetch error: {e}")
        return out
    # Drop events already over; all-day events sort to the top of their day.
    def key(ev):
        return (ev["start"] or "")[:10], 0 if ev["all_day"] else 1, ev["start"] or ""
    upcoming = [ev for ev in events if not ev["end"] or ev["all_day"] or ev["end"] > now.isoformat()]
    out["events"] = sorted(upcoming, key=key)[:limit]
    _cal_cache.update(ts=now_ts, data=out)
    return out


async def music_now() -> Dict[str, Any]:
    """Now playing + the last few scrobbles from Last.fm."""
    if not (LASTFM_USER and LASTFM_API_KEY):
        return {"configured": False, "now_playing": None, "recent": []}
    now_ts = time.time()
    if _music_cache["data"] is not None and now_ts - _music_cache["ts"] < MUSIC_TTL:
        return _music_cache["data"]
    out: Dict[str, Any] = {"configured": True, "now_playing": None, "recent": [], "ok": False}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("https://ws.audioscrobbler.com/2.0/", params={
                "method": "user.getrecenttracks", "user": LASTFM_USER, "api_key": LASTFM_API_KEY,
                "format": "json", "limit": 6,
            })
            r.raise_for_status()
            tracks = ((r.json() or {}).get("recenttracks") or {}).get("track") or []
    except Exception as e:
        print(f"last.fm fetch error: {e}")
        return out
    for t in tracks:
        img = next((i.get("#text") for i in reversed(t.get("image") or []) if i.get("#text")), None)
        item = {
            "track": t.get("name"),
            "artist": (t.get("artist") or {}).get("#text"),
            "album": (t.get("album") or {}).get("#text"),
            "art": img,
            "uts": int((t.get("date") or {}).get("uts") or 0) or None,
        }
        if (t.get("@attr") or {}).get("nowplaying") == "true" and out["now_playing"] is None:
            out["now_playing"] = item
        else:
            out["recent"].append(item)
    out["recent"] = out["recent"][:5]
    out["ok"] = True
    _music_cache.update(ts=now_ts, data=out)
    return out
