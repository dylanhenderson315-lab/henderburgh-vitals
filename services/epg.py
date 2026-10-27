"""Full cable listings for the channel guide, free: EPGTalk's US XMLTV feed.

EPGTalk (github.com/acidjesuz/EPGTalk, free for legal personal use)
publishes Gracenote-sourced US listings as XMLTV; the "lite" file covers 48
hours and refreshes about every 6 hours. Each HTC channel below is pinned to
one feed channel by its exact XMLTV id (the East / standard-definition feed,
so times match Eastern cable). Channels with no feed entry (the Myrtle
Beach locals, MS NOW, a few diginets) fall back to TVmaze in services/guide.py.

Parsing is streamed (iterparse, elements cleared as we go) and keeps only
mapped channels inside the window, so memory stays small.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

URL = "https://raw.githubusercontent.com/acidjesuz/EPGTalk/master/US_lite.xml.gz"
REFRESH_S = 3 * 3600

# HTC channel -> XMLTV channel id (checked against the feed 2026-10-27).
SD = ".schedulesdirect.org"
GN = ".gracenote.com"
FEED = {
    3: "I204.10145" + SD, 5: "I311.10093" + SD, 6: "I350.10161" + SD, 10: "I9110.109718" + SD,   # NBC: network East feed
    11: "I275.11069" + SD, 18: "I240.62077" + SD, 19: "I315.82682" + SD, 20: "I80.78851" + SD,
    22: "I372.59539" + SD, 23: "I252.10918" + SD, 24: "I229.14902" + SD, 25: "I245.11164" + SD,
    26: "I202.58646" + SD, 27: "I242.11207" + SD, 28: "I206.10179" + SD, 29: "I209.12444" + SD,
    30: "I253.18480" + SD, 31: "I278.11150" + SD, 32: "I335.11218" + SD, 33: "I329.10051" + SD,
    34: "I249.10149" + SD, 35: "I331.10986" + SD, 36: "I362.11187" + SD, 37: "I290.10171" + SD,
    38: "I280.11158" + SD, 39: "I305.76894" + SD, 40: "I244.11097" + SD, 41: "I296.12131" + SD,
    42: "I355.10139" + SD, 43: "I265.10035" + SD, 44: "I327.10138" + SD, 45: "I256.12852" + SD,
    46: "I254.10021" + SD, 47: "I247.11867" + SD, 50: "I236.10989" + SD, 51: "I269.14771" + SD,
    52: "I218.14899" + SD, 53: "I299.11006" + SD, 54: "I304.16123" + SD, 55: "I219.82541" + SD,
    56: "I246.10153" + SD, 57: "I282.16331" + SD, 58: "I277.11180" + SD, 59: "I279.70387" + SD,
    60: "I248.14321" + SD, 61: "I360.16374" + SD, 62: "I231.12574" + SD, 63: "I241.11163" + SD,
    64: "I276.24959" + SD, 65: "I237.10057" + SD, 66: "I359.58649" + SD, 67: "I612.111905" + SD,
    68: "I307.91096" + SD, 71: "I285.16615" + SD, 72: "I259.17927" + SD, 73: "I611.89535" + SD,
    76: "I217.33395" + SD, 77: "I251.21484" + SD, 78: "I312.11221" + SD, 79: "I565.46710" + SD,
    81: "I260.16409" + SD, 83: "I264.18332" + SD, 84: "I239.92091" + GN, 86: "I56.2.45325" + GN,
    88: "I24.3.46785" + GN, 91: "I77.70436" + SD, 92: "I82.73067" + SD, 94: "I81.89922" + SD,
    96: "I221.16365" + SD, 97: "I213.62079" + SD, 99: "I313.16604" + SD, 136: "I273.16715" + SD,
}
BY_ID = {v: k for k, v in FEED.items()}

_state: Dict[str, Any] = {"ts": 0.0, "data": {}, "loading": None}
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿]")
_TAGS = re.compile(r"\s*\[(NEW|LIVE|[A-Z]{2,5})\]")


def _t(s: Optional[str]) -> Optional[datetime]:
    """XMLTV '20261027020000 +0000' -> aware datetime."""
    try:
        return datetime.strptime(s, "%Y%m%d%H%M%S %z")
    except Exception:
        return None


def _clean(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = _TAGS.sub("", _EMOJI.sub("", s))
    return re.sub(r"\s{2,}", " ", s).strip() or None


def _parse(raw: bytes, t0: datetime, t1: datetime) -> Dict[int, List[Dict[str, Any]]]:
    out: Dict[int, List[Dict[str, Any]]] = {}
    for _, e in ET.iterparse(io.BytesIO(gzip.decompress(raw)), events=("end",)):
        if e.tag != "programme":
            if e.tag == "channel":
                e.clear()
            continue
        ch = BY_ID.get(e.get("channel"))
        if ch:
            s, en = _t(e.get("start")), _t(e.get("stop"))
            if s and en and en > t0 and s < t1:
                title_raw = e.findtext("title") or ""
                cats = [c.text for c in e.findall("category") if c.text]
                out.setdefault(ch, []).append({
                    "start": s.isoformat(), "end": en.isoformat(),
                    "title": _clean(title_raw) or "Untitled", "sub": _clean(e.findtext("sub-title")),
                    "kind": "sports" if any(c in ("Football", "Basketball", "Baseball", "Hockey", "Soccer", "Golf", "Tennis",
                                                  "Auto racing", "Boxing", "Mixed martial arts", "Sports event") for c in cats) else "show",
                    "genre": cats[0] if cats else None,
                    "live": e.find("live") is not None or "[LIVE]" in title_raw,
                    "new": e.find("new") is not None or "[NEW]" in title_raw,
                    "apps": [],
                })
        e.clear()
    for v in out.values():
        v.sort(key=lambda p: p["start"])
    return out


async def _refresh() -> None:
    try:
        async with httpx.AsyncClient(timeout=40.0, follow_redirects=True, headers={"User-Agent": "henderburgh-hub/1.0"}) as c:
            r = await c.get(URL)
            r.raise_for_status()
            raw = r.content
        now = datetime.now(timezone.utc)
        data = await asyncio.to_thread(_parse, raw, now - timedelta(hours=3), now + timedelta(hours=30))
        if data:
            _state.update(ts=time.time(), data=data)
    except Exception as e:
        print(f"epg refresh error: {e}")
    finally:
        _state["loading"] = None


async def listings() -> Dict[int, List[Dict[str, Any]]]:
    """HTC channel -> programmes. First call waits for the download; after
    that a stale copy is served while a refresh runs in the background."""
    fresh = time.time() - _state["ts"] < REFRESH_S
    if not fresh and _state["loading"] is None:
        _state["loading"] = asyncio.ensure_future(_refresh())
    if not _state["data"] and _state["loading"] is not None:
        try:
            await asyncio.wait_for(asyncio.shield(_state["loading"]), 30)
        except Exception:
            pass
    return _state["data"]
