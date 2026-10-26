"""Sunrise / sunset for the house, so the hub's "right now" follows the real
sky instead of a fixed clock (Evening suggested from just before sunset).

Coordinates come from Home Assistant's own config (the house's location),
fetched once a day and never exposed — only the times leave this module.
The maths is NOAA's standard solar calculation (accurate to about a minute
at these latitudes); no third-party dependency.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import httpx

from config import HA_ENABLED, HA_HEADERS, HA_URL

ET = ZoneInfo("America/New_York")
_coords: Dict[str, Any] = {"ts": 0.0, "lat": None, "lon": None}


def sun_times(lat: float, lon: float, d: date) -> Dict[str, Optional[datetime]]:
    """Sunrise and sunset (UTC) for a date, NOAA solar position method."""
    n = d.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma) - 0.006758 * math.cos(2 * gamma)
            + 0.000907 * math.sin(2 * gamma) - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    phi = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl)) - math.tan(phi) * math.tan(decl)
    if not -1 <= cos_ha <= 1:
        return {"sunrise": None, "sunset": None}   # polar day/night — not a concern here, but honest
    ha = math.degrees(math.acos(cos_ha))
    midnight = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    rise = 720 - 4 * (lon + ha) - eqtime
    sset = 720 - 4 * (lon - ha) - eqtime
    return {"sunrise": midnight + timedelta(minutes=rise), "sunset": midnight + timedelta(minutes=sset)}


async def _home_coords() -> Optional[tuple]:
    if _coords["lat"] is not None and time.time() - _coords["ts"] < 86400:
        return _coords["lat"], _coords["lon"]
    if not HA_ENABLED:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{HA_URL}/api/config", headers=HA_HEADERS)
            r.raise_for_status()
            cfg = r.json() or {}
        _coords.update(ts=time.time(), lat=float(cfg["latitude"]), lon=float(cfg["longitude"]))
    except Exception as e:
        print(f"sky coords error: {e}")
        return (_coords["lat"], _coords["lon"]) if _coords["lat"] is not None else None
    return _coords["lat"], _coords["lon"]


async def today() -> Optional[Dict[str, Any]]:
    """Today's sunrise/sunset in ET, and whether the sun is up right now."""
    c = await _home_coords()
    if not c:
        return None
    now = datetime.now(ET)
    t = sun_times(c[0], c[1], now.date())
    if not t["sunset"]:
        return None
    rise, sset = t["sunrise"].astimezone(ET), t["sunset"].astimezone(ET)
    return {"sunrise": rise.isoformat(), "sunset": sset.isoformat(), "is_day": rise <= now <= sset}
