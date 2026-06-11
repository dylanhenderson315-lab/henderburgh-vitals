"""
HENDER VITALS Dashboard
Beautiful personal Oura Ring dashboard — FastAPI + HTMX + Tailwind + Chart.js
Run with: uvicorn main:app --reload
"""

from __future__ import annotations

import os
import asyncio
import json
import uuid
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Body, FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

load_dotenv()

# =============================================================================
# Configuration
# =============================================================================
OURA_TOKEN = os.getenv("OURA_TOKEN", "").strip()
OURA_DAYS = int(os.getenv("OURA_DAYS", "14"))
OURA_BASE_URL = "https://api.ouraring.com/v2"

# Public mode configuration (for henderburgh.com deployment)
PUBLIC_MODE = os.getenv("PUBLIC_MODE", "false").lower() in ("1", "true", "yes")
# Always uppercase HENDERBURGH for consistent all-caps branding across the site
DISPLAY_NAME = (os.getenv("DISPLAY_NAME", "HENDERBURGH").strip() or "HENDERBURGH").upper()
SITE_NAME = (os.getenv("SITE_NAME", "HENDERBURGH").strip() or "HENDERBURGH").upper()
SITE_URL = os.getenv("SITE_URL", "https://henderburgh.com")

# Longer cache in public mode to protect Oura API quota
DEFAULT_CACHE_TTL = 600 if PUBLIC_MODE else 180  # 10 min public, 3 min local
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL))

# Special shorter cache for heart rate (can be more frequent)
HEARTRATE_CACHE_TTL = int(os.getenv("HEARTRATE_CACHE_TTL", "120" if PUBLIC_MODE else "30"))  # 2 min public, 30s local

# Railway (and other platforms) inject PORT. Default to 8000 for local dev.
PORT = int(os.getenv("PORT", 8000))

# Auto-refresh interval in seconds (0 or negative = disabled)
AUTO_REFRESH_SECONDS = int(os.getenv("AUTO_REFRESH_SECONDS", "900"))  # 15 min sensible default (Oura data is not real-time); override via env for faster in dev if desired

# Admin token for protected actions (e.g. deleting blog messages)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# Xbox (OpenXBL) configuration for Live Now section
# XBL_API_KEY and XBL_GAMERTAG must be set in Railway (production) or .env (local) for this to work.
# See .env.example for setup instructions.
# XBL_XUID is now set (from xbl.io /account lookup) to bypass gamertag profile resolution (the /gamertag endpoint was returning 500 NOT_FOUND even with valid key).
#
# Placeholder detection: we only consider the values as "not configured" if they exactly match
# the example placeholder strings from .env.example ("your_real_xbl_key_here" or "your_exact_gamertag_here").
# This way, real keys (even if they look like the old default) or the actual gamertag "NutNutBiinks"
# will trigger real API calls. The old default key in the getenv() fallback is kept for backward compat
# but will now attempt API (and fail gracefully) instead of forcing not_configured.
XBL_API_KEY = os.getenv("XBL_API_KEY", "7ede4621-fd2d-4928-919e-8f520a85804d")
XBL_GAMERTAG = os.getenv("XBL_GAMERTAG", "NutNutBiinks")
XBL_XUID = os.getenv("XBL_XUID", "")  # Optional: if set, skip gamertag resolution and use this XUID directly for presence call

# Xbox response caching to protect the free-tier 150 req/hour limit on xbl.io.
# We cache successful profile+presence+account responses for ~5 minutes.
# This means at most ~12 actual API roundtrips per hour per server process (even with many visitors),
# plus we always serve the last-known-good data instantly on cache hits or on any failure/rate-limit.
XBOX_CACHE_TTL_SECONDS = 5 * 60

# Simple in-memory last vitals snapshot for instant structure on /vitals (like HA bootstrap).
# Allows the page to feel instantly populated with last known scores/metrics + freshness, then background hydrate.
last_vitals_snapshot = {"data": None, "ts": 0}

# Warn at startup ONLY if using the example placeholder strings (not real values)
if XBL_API_KEY in ("your_real_xbl_key_here", "your_openxbl_api_key_here") or XBL_GAMERTAG in ("your_gamertag_here", "your_exact_gamertag_here"):
    print("⚠️  WARNING: Using placeholder XBL_API_KEY / XBL_GAMERTAG. Xbox status will be unavailable until you set real values in .env or Railway env vars. See .env.example for instructions.")

# Admin token used for protected actions (e.g. deleting messages on /blog)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# =============================================================================
# Home Assistant Lighting - instant bootstrap + last known state cache (for premium fast feel)
# Persisted structure (rooms + sync groups) from lighting_config.json is always shown immediately.
# Fresh HA data fetched in background; only changed states trigger UI updates.
# =============================================================================
last_ha_lights_snapshot: Dict[str, Any] = {"data": None, "ts": 0}  # {lights: {eid: {state, brightness, current_effect, ...}}, ts, total}
HA_BOOTSTRAP_CACHE_TTL = 45  # seconds; background refresh keeps it fresh for "last known"

# In-memory cache for last successful Xbox status (survives between requests in the same process)
# We only hit xbl.io when this cache is older than XBOX_CACHE_TTL_SECONDS.
# On any error (including rate limits) we return the previous last_xbox_data so the UI stays useful.
last_xbox_data = {"status": "unavailable", "state": "Unknown", "game": "—", "last_updated": 0}
_last_xbox_fetch_time: float = 0.0  # epoch seconds of the last *successful* network fetch (for cache decision)

# =============================================================================
# Home Assistant (local only, disabled in PUBLIC_MODE)
# Connection is to a local instance; only meaningful when running on the LAN.
# Controls are protected by ADMIN_TOKEN (same as blog).
# =============================================================================
HA_URL = os.getenv("HA_URL", "http://192.168.40.203:8123").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxYjI4YzRmNWEyYzE0NTNlYTA3MmI1MjE2N2RjMTM1ZSIsImlhdCI6MTc4MDk3MzU4NCwiZXhwIjoyMDk2MzMzNTg0fQ.vu-g86YpAOhNfTxnYKwj0peETr6_iqctfoOks8q1iHM")
HA_ENABLED = (not PUBLIC_MODE) and bool(HA_TOKEN) and bool(HA_URL)

HA_HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
} if HA_TOKEN else {}

if not PUBLIC_MODE and not HA_TOKEN:
    print("⚠️  HA_TOKEN not set. Home Assistant features will be unavailable.")

if PUBLIC_MODE and HA_TOKEN:
    print("ℹ️  HA integration loaded but disabled because PUBLIC_MODE=true (controls hidden).")

OFFICE_LAMP = "light.office_lamp"
_poke_last = 0.0
_notify_last = 0.0

# =============================================================================

if PUBLIC_MODE:
    if not OURA_TOKEN:
        raise RuntimeError(
            "PUBLIC_MODE is enabled but OURA_TOKEN is not set. "
            "Set OURA_TOKEN as an environment variable on your hosting platform."
        )
    print(f"🌍 Running in PUBLIC_MODE for {SITE_URL} — token required, aggressive caching enabled.")
else:
    if not OURA_TOKEN:
        print("⚠️  OURA_TOKEN not set. Dashboard will show setup instructions.")

# =============================================================================
# Simple Rate Limiter (for public deployments)
# =============================================================================
class SimpleRateLimiter:
    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self.requests: Dict[str, List[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window_start = now - self.window
        # Clean old entries
        self.requests[key] = [t for t in self.requests[key] if t > window_start]
        if len(self.requests[key]) >= self.max_requests:
            return False
        self.requests[key].append(now)
        return True

# In public mode we are more protective
rate_limiter = SimpleRateLimiter(
    max_requests=40 if PUBLIC_MODE else 120,
    window_seconds=60
)

# =============================================================================
# Oura Client
# =============================================================================
class OuraClient:
    def __init__(self, token: str):
        self.token = token
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: Dict[str, tuple[float, Any]] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=OURA_BASE_URL,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=30.0,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()

    def _cache_key(self, path: str, params: Dict) -> str:
        return f"{path}?{sorted(params.items())}"

    async def _get(self, path: str, params: Optional[Dict] = None, bypass_cache: bool = False, custom_ttl: Optional[int] = None) -> Dict[str, Any]:
        params = params or {}
        key = self._cache_key(path, params)
        now = asyncio.get_event_loop().time()

        ttl = custom_ttl if custom_ttl is not None else CACHE_TTL_SECONDS

        if not bypass_cache and key in self._cache:
            ts, data = self._cache[key]
            if now - ts < ttl:
                return data

        client = await self._get_client()
        resp = await client.get(path, params=params)
        if resp.status_code == 401:
            raise HTTPException(401, "Invalid or expired Oura token")
        if resp.status_code == 403:
            raise HTTPException(403, "Oura API access forbidden (subscription or permissions)")
        resp.raise_for_status()
        data = resp.json()
        self._cache[key] = (now, data)
        return data

    # --- Endpoints -----------------------------------------------------------------

    async def get_personal_info(self) -> Dict[str, Any]:
        return await self._get("/usercollection/personal_info")

    async def get_daily_readiness(self, start: str, end: str) -> List[Dict]:
        data = await self._get("/usercollection/daily_readiness", {"start_date": start, "end_date": end})
        return data.get("data", [])

    async def get_daily_sleep(self, start: str, end: str) -> List[Dict]:
        data = await self._get("/usercollection/daily_sleep", {"start_date": start, "end_date": end})
        return data.get("data", [])

    async def get_sleep(self, start: str, end: str) -> List[Dict]:
        """Detailed sleep (contains avg_hrv, avg_breath, lowest_hr, etc.)"""
        data = await self._get("/usercollection/sleep", {"start_date": start, "end_date": end})
        return data.get("data", [])

    async def get_daily_activity(self, start: str, end: str) -> List[Dict]:
        data = await self._get("/usercollection/daily_activity", {"start_date": start, "end_date": end})
        return data.get("data", [])

    async def get_daily_spo2(self, start: str, end: str) -> List[Dict]:
        data = await self._get("/usercollection/daily_spo2", {"start_date": start, "end_date": end})
        return data.get("data", [])

    async def get_heartrate(self, start: str, end: str, bypass_cache: bool = False) -> List[Dict]:
        """Fetch recent heart rate readings. Uses shorter cache than other metrics."""
        try:
            # Use shorter TTL for heartrate
            original_ttl = CACHE_TTL_SECONDS
            # Temporarily override for this call if not bypassing
            if not bypass_cache:
                # We can't easily override global, so we pass bypass if we want fresh
                # For now, the fast path uses bypass_cache=True
                pass

            data = await self._get(
                "/usercollection/heartrate", 
                {"start_date": start, "end_date": end},
                bypass_cache=bypass_cache,
                custom_ttl=HEARTRATE_CACHE_TTL
            )
            return data.get("data", [])
        except Exception:
            return []

    async def get_daily_stress(self, start: str, end: str) -> List[Dict]:
        data = await self._get("/usercollection/daily_stress", {"start_date": start, "end_date": end})
        return data.get("data", [])

    async def get_daily_cardiovascular_age(self, start: str, end: str) -> List[Dict]:
        try:
            data = await self._get("/usercollection/daily_cardiovascular_age", {"start_date": start, "end_date": end})
            return data.get("data", [])
        except Exception:
            return []

    async def get_daily_resilience(self, start: str, end: str) -> List[Dict]:
        try:
            data = await self._get("/usercollection/daily_resilience", {"start_date": start, "end_date": end})
            return data.get("data", [])
        except Exception:
            return []

    async def get_workouts(self, start: str, end: str) -> List[Dict]:
        """Detailed activity sessions (workouts, walks, runs etc). Best source for type/start/duration/distance."""
        try:
            data = await self._get("/usercollection/workout", {"start_date": start, "end_date": end})
            return data.get("data", [])
        except Exception:
            return []


# =============================================================================
# Data Processing Helpers
# =============================================================================
def _parse_date(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()

def _fmt_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "—"
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hrs}h {mins}m" if hrs else f"{mins}m"

def _fmt_decimal(val: Optional[float], ndigits: int = 1) -> str:
    if val is None:
        return "—"
    return f"{round(val, ndigits)}"

def _safe_get(d: Dict, path: str, default: Any = None) -> Any:
    cur = d
    for k in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    return cur if cur is not None else default

def compute_trend(current: Optional[float], previous: Optional[float], invert: bool = False) -> Dict[str, Any]:
    """Return {arrow, pct, color} for display."""
    if current is None or previous is None or previous == 0:
        return {"arrow": "", "pct": "", "color": "zinc", "dir": 0}

    delta = current - previous
    pct = abs(delta / previous * 100)
    dir = 1 if delta > 0 else (-1 if delta < 0 else 0)

    if invert:
        dir = -dir  # e.g. lower RHR is good

    if dir > 0:
        arrow, color = "↑", "emerald"
    elif dir < 0:
        arrow, color = "↓", "rose"
    else:
        arrow, color = "→", "zinc"

    return {
        "arrow": arrow,
        "pct": f"{pct:.0f}%",
        "color": color,
        "dir": dir,
    }

def get_date_range(days: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()

def process_dashboard_data(
    personal: Dict,
    readiness: List[Dict],
    daily_sleep: List[Dict],
    detailed_sleep: List[Dict],
    activity: List[Dict],
    spo2: List[Dict],
    stress: List[Dict],
    heartrate: List[Dict],
    workouts: List[Dict],
    days: int,
) -> Dict[str, Any]:
    """Turn raw API responses into a clean dashboard context dict."""
    today = date.today().isoformat()

    # --- Latest values (most recent day with data) ---
    latest_readiness = next((r for r in reversed(readiness) if r.get("score") is not None), None)
    latest_daily_sleep = next((s for s in reversed(daily_sleep) if s.get("score") is not None), None)

    # Find matching detailed sleep for the same day
    latest_detailed = None
    if latest_daily_sleep:
        day = latest_daily_sleep.get("day")
        latest_detailed = next((s for s in reversed(detailed_sleep) if s.get("day") == day), None)

    latest_activity = next((a for a in reversed(activity) if a.get("score") is not None), None)
    latest_spo2 = next((s for s in reversed(spo2) if s.get("spo2_percentage") is not None), None)
    latest_stress = next((s for s in reversed(stress) if s.get("day_summary")), None)

    # Handle SpO2 which can be a number or an object like {"average": 95.8}
    spo2_raw = _safe_get(latest_spo2, "spo2_percentage")
    if isinstance(spo2_raw, dict):
        spo2_val = spo2_raw.get("average")
    else:
        spo2_val = spo2_raw
    if spo2_val is not None:
        try:
            spo2_val = round(float(spo2_val))
        except (ValueError, TypeError):
            spo2_val = None

    # Physiological values (prefer detailed sleep)
    hrv = _safe_get(latest_detailed, "average_hrv")
    rhr = _safe_get(latest_detailed, "lowest_heart_rate") or _safe_get(latest_detailed, "average_heart_rate")
    resp_rate = _safe_get(latest_detailed, "average_breath")
    sleep_efficiency = _safe_get(latest_detailed, "efficiency") or _safe_get(latest_daily_sleep, "contributors.efficiency")

    # Latest heart rate from the heartrate endpoint (most recent reading)
    latest_hr = None
    latest_hr_timestamp = None
    if heartrate:
        # Search from most recent
        for entry in reversed(heartrate):
            if isinstance(entry, dict) and entry.get("bpm") is not None:
                latest_hr = entry.get("bpm")
                latest_hr_timestamp = entry.get("timestamp")
                break
    # Fallback to resting HR if no heartrate samples
    if latest_hr is None:
        latest_hr = rhr
        latest_hr_timestamp = None  # no precise timestamp for fallback

    # Compute how old the heart rate reading is (in minutes)
    hr_age_minutes = None
    if latest_hr_timestamp:
        try:
            ts = datetime.fromisoformat(latest_hr_timestamp.replace("Z", "+00:00"))
            hr_age_minutes = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
        except Exception:
            hr_age_minutes = None

    # Temperature from readiness
    temp_dev = _safe_get(latest_readiness, "temperature_deviation")

    # --- Build time series (last N days) ---
    # Index by day for easy joining
    def by_day(items: List[Dict]) -> Dict[str, Dict]:
        return {item["day"]: item for item in items if item.get("day")}

    r_by_day = by_day(readiness)
    ds_by_day = by_day(daily_sleep)
    det_by_day = by_day(detailed_sleep)
    act_by_day = by_day(activity)
    spo_by_day = by_day(spo2)

    # Generate last `days` dates (most recent first for display, but we'll reverse for charts)
    end_date = date.today()
    series_dates: List[str] = []
    for i in range(days - 1, -1, -1):
        d = (end_date - timedelta(days=i)).isoformat()
        series_dates.append(d)

    # Series data
    hrv_series: List[Optional[float]] = []
    sleep_score_series: List[Optional[int]] = []
    sleep_dur_series: List[Optional[float]] = []  # hours
    rhr_series: List[Optional[float]] = []
    readiness_series: List[Optional[int]] = []
    temp_series: List[Optional[float]] = []

    for d in series_dates:
        det = det_by_day.get(d, {})
        ds = ds_by_day.get(d, {})
        r = r_by_day.get(d, {})

        hrv_series.append(det.get("average_hrv"))
        sleep_score_series.append(ds.get("score"))
        dur = det.get("total_sleep_duration") or 0
        sleep_dur_series.append(round(dur / 3600, 1) if dur else None)
        rhr_series.append(det.get("lowest_heart_rate") or det.get("average_heart_rate"))
        readiness_series.append(r.get("score"))
        temp_series.append(r.get("temperature_deviation"))

    # --- Trends (today vs yesterday) ---
    def prev_value(series: List[Optional[float]]) -> Optional[float]:
        vals = [v for v in series if v is not None]
        return vals[-2] if len(vals) >= 2 else None

    def last_value(series: List[Optional[float]]) -> Optional[float]:
        vals = [v for v in series if v is not None]
        return vals[-1] if vals else None

    readiness_trend = compute_trend(
        last_value(readiness_series),
        prev_value(readiness_series),
    )
    sleep_score_trend = compute_trend(
        last_value(sleep_score_series),
        prev_value(sleep_score_series),
    )
    hrv_trend = compute_trend(
        last_value(hrv_series),
        prev_value(hrv_series),
    )
    rhr_trend = compute_trend(
        last_value(rhr_series),
        prev_value(rhr_series),
        invert=True,  # lower is better
    )

    # Sleep duration trend (hours)
    sleep_dur_trend = compute_trend(
        last_value(sleep_dur_series),
        prev_value(sleep_dur_series),
    )

    # --- Current display values ---
    total_sleep = _safe_get(latest_detailed, "total_sleep_duration")
    deep = _safe_get(latest_detailed, "deep_sleep_duration")
    rem = _safe_get(latest_detailed, "rem_sleep_duration")
    light = _safe_get(latest_detailed, "light_sleep_duration")

    # Activity
    steps = _safe_get(latest_activity, "steps")
    active_cal = _safe_get(latest_activity, "active_calories")

    # Name (upper for branding consistency; greeting phrases hardcode HENDERBURGH)
    name = DISPLAY_NAME or (personal.get("name") or personal.get("email", "there").split("@")[0]).upper()

    # Time-based greeting — always HENDERBURGH in ALL CAPS (Eastern Time)
    local_tz = ZoneInfo("America/New_York")
    local_now = datetime.now(local_tz)
    h = local_now.hour
    m = local_now.minute
    total_minutes = h * 60 + m

    if 7 * 60 <= total_minutes < 8 * 60 + 30:
        time_greeting = "HENDERBURGH IS WAKING UP."
    elif 8 * 60 + 30 <= total_minutes < 17 * 60:
        time_greeting = "HENDERBURGH IS WORKING."
    elif 17 * 60 <= total_minutes < 22 * 60:
        time_greeting = "HENDERBURGH IS RELAXING."
    elif 22 * 60 <= total_minutes < 24 * 60:
        time_greeting = "HENDERBURGH IS GETTING READY FOR BED."
    else:
        time_greeting = "HENDERBURGH IS SLEEPING."

    # --- Recent detailed activity sessions (Recent Activity section) ---
    # Pull real per-session data (type, start, duration, distance etc) from workouts collection
    recent_activities: List[Dict[str, Any]] = []
    if workouts:
        try:
            sorted_ws = sorted(
                [w for w in workouts if w.get("start_time") and w.get("end_time")],
                key=lambda w: w.get("start_time", ""),
                reverse=True,
            )
            for w in sorted_ws[:6]:
                try:
                    start_iso = w.get("start_time")
                    end_iso = w.get("end_time")
                    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                    local_start = start_dt.astimezone(local_tz)
                    dur_sec = int((end_dt - start_dt).total_seconds())
                    if dur_sec < 60:
                        continue
                    # Nice activity type label
                    raw_act = (w.get("activity") or w.get("type") or "activity").lower().replace("_", " ")
                    type_map = {
                        "walking": "Walk",
                        "running": "Run",
                        "cycling": "Cycle",
                        "swimming": "Swim",
                        "strength training": "Strength",
                        "yoga": "Yoga",
                        "pilates": "Pilates",
                        "hiit": "HIIT",
                        "workout": "Workout",
                        "other": "Activity",
                    }
                    act_type = type_map.get(raw_act, raw_act.title())
                    # Start time in Eastern, clean e.g. 2:45 PM (strip leading zero)
                    start_str = local_start.strftime("%I:%M %p").lstrip("0")  # e.g. 9:05 AM or 10:30 AM
                    # Duration
                    dur_min = dur_sec // 60
                    dur_str = f"{dur_min}m" if dur_min < 60 else f"{dur_min // 60}h {dur_min % 60}m"
                    # Distance (meters -> miles, US style)
                    dist_m = w.get("distance") or 0
                    dist_str = None
                    if dist_m and dist_m > 50:
                        miles = round(dist_m / 1609.34, 1)
                        dist_str = f"{miles} mi"
                    steps = w.get("steps")
                    cals = w.get("calories") or w.get("active_calories")
                    recent_activities.append({
                        "type": act_type,
                        "start_time": start_str,
                        "duration": dur_str,
                        "distance": dist_str,
                        "steps": steps if steps else None,
                        "calories": cals if cals else None,
                    })
                except Exception:
                    continue
        except Exception:
            recent_activities = []

    now = datetime.now(ZoneInfo("UTC"))

    return {
        "name": name,
        "last_updated": now.strftime("%b %d, %H:%M UTC"),
        "last_updated_iso": now.isoformat(),
        "now": now,
        "time_greeting": time_greeting,
        "days": days,
        "today": today,

        # Hero / main scores
        "readiness_score": _safe_get(latest_readiness, "score"),
        "sleep_score": _safe_get(latest_daily_sleep, "score"),
        "activity_score": _safe_get(latest_activity, "score"),

        # Physiological cards
        "hrv": hrv,
        "rhr": rhr,
        "respiratory_rate": resp_rate,
        "spo2": spo2_val,
        "temp_deviation": temp_dev,
        "stress_summary": _safe_get(latest_stress, "day_summary"),
        "latest_hr": latest_hr,
        "latest_hr_timestamp": latest_hr_timestamp,
        "hr_age_minutes": hr_age_minutes,

        # Sleep breakdown
        "total_sleep": _fmt_duration(total_sleep),
        "deep_sleep": _fmt_duration(deep),
        "rem_sleep": _fmt_duration(rem),
        "light_sleep": _fmt_duration(light),
        "sleep_efficiency": sleep_efficiency,

        # Activity
        "steps": steps,
        "active_calories": active_cal,

        # Recent detailed sessions (new "Recent Activity" section)
        "recent_activities": recent_activities,

        # Trends
        "readiness_trend": readiness_trend,
        "sleep_score_trend": sleep_score_trend,
        "hrv_trend": hrv_trend,
        "rhr_trend": rhr_trend,
        "sleep_dur_trend": sleep_dur_trend,

        # Chart data (for Chart.js)
        "chart": {
            "labels": series_dates,
            "hrv": hrv_series,
            "sleep_score": sleep_score_series,
            "sleep_duration": sleep_dur_series,
            "rhr": rhr_series,
            "readiness": readiness_series,
            "temp": temp_series,
        },
    }


# =============================================================================
# FastAPI App
# =============================================================================
app = FastAPI(title="HENDERBURGH", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="templates")

# Serve static assets (e.g. your floor plan blueprint image as visual reference in /home-assistant).
# Put the image at static/floorplan.jpg (or update the <img src> in the template).
# The floor plan view uses it as context while the main organize view (with drag/drop, sync groups, detail panels) stays clean/fast.
import os
from fastapi.staticfiles import StaticFiles
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Global client (simple singleton for local dashboard)
oura_client: Optional[OuraClient] = None


@app.on_event("startup")
async def startup():
    global oura_client
    if OURA_TOKEN:
        oura_client = OuraClient(OURA_TOKEN)
    elif PUBLIC_MODE:
        # Should never reach here because of the earlier RuntimeError
        raise RuntimeError("PUBLIC_MODE requires OURA_TOKEN")


@app.on_event("shutdown")
async def shutdown():
    global oura_client
    if oura_client:
        await oura_client.close()


async def fetch_all_data(days: int = OURA_DAYS) -> Dict[str, Any]:
    """Fetch everything we need in parallel."""
    if not oura_client:
        raise HTTPException(400, "OURA_TOKEN not configured")

    start, end = get_date_range(days)

    personal, readiness, daily_sleep, detailed_sleep, activity, spo2, stress, heartrate, workouts = await asyncio.gather(
        oura_client.get_personal_info(),
        oura_client.get_daily_readiness(start, end),
        oura_client.get_daily_sleep(start, end),
        oura_client.get_sleep(start, end),
        oura_client.get_daily_activity(start, end),
        oura_client.get_daily_spo2(start, end),
        oura_client.get_daily_stress(start, end),
        oura_client.get_heartrate(start, end, bypass_cache=True),  # Always get fresh HR on dashboard load
        oura_client.get_workouts(start, end),
        return_exceptions=True,
    )

    # Handle any failures gracefully
    def safe(val, default=None):
        return val if not isinstance(val, Exception) else default

    return {
        "personal": safe(personal, {}),
        "readiness": safe(readiness, []),
        "daily_sleep": safe(daily_sleep, []),
        "detailed_sleep": safe(detailed_sleep, []),
        "activity": safe(activity, []),
        "spo2": safe(spo2, []),
        "stress": safe(stress, []),
        "heartrate": safe(heartrate, []),
        "workouts": safe(workouts, []),
    }


# =============================================================================
# Routes
# =============================================================================
def _render(template_name: str, context: Dict[str, Any]) -> HTMLResponse:
    """Render a Jinja template safely (works around Jinja2Templates cache key bugs on some Python versions)."""
    template = templates.env.get_template(template_name)
    html = template.render(**context)
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Clean minimalist home page for HENDERBURGH."""
    steps = None
    latest_hr = None
    hr_age_minutes = None
    latest_hr_timestamp = None

    if oura_client:
        try:
            # Use the exact same day window as the vitals page for data consistency
            days = 14 if PUBLIC_MODE else OURA_DAYS

            raw = await fetch_all_data(days=days)

            processed = process_dashboard_data(
                raw.get("personal", {}),
                raw.get("readiness", []),
                raw.get("daily_sleep", []),
                raw.get("detailed_sleep", []),
                raw.get("activity", []),
                raw.get("spo2", []),
                raw.get("stress", []),
                raw.get("heartrate", []),
                raw.get("workouts", []),
                days,
            )

            steps = processed.get("steps")
            latest_hr = processed.get("latest_hr")
            hr_age_minutes = processed.get("hr_age_minutes")
            latest_hr_timestamp = processed.get("latest_hr_timestamp")

        except Exception:
            pass

    # Prepare context for homepage (same values as vitals page)
    steps_ctx = {
        "count": steps,
        "miles": round(steps * 0.0005, 1) if steps is not None else None,
    }

    # Format "updated X ago" the same way as vitals
    hr_updated_ago = "recently"
    if latest_hr_timestamp and hr_age_minutes is not None:
        try:
            ts = datetime.fromisoformat(latest_hr_timestamp.replace("Z", "+00:00"))
            local_tz = ZoneInfo("America/New_York")
            local_time = ts.astimezone(local_tz)

            if hr_age_minutes < 60:
                hr_updated_ago = f"updated {hr_age_minutes}m ago"
            else:
                hr_updated_ago = local_time.strftime("updated at %-I:%M %p").replace(" 0", " ")
        except Exception:
            hr_updated_ago = "recently"

    hr_ctx = {
        "bpm": latest_hr,
        "updated_ago": hr_updated_ago,
    }

    # Blog content for the home hub: recent top-level posts (neat teaser) + unread count for the lamp poke notifier.
    # We surface actual blog activity directly on the main screen as first-class content.
    recent_messages = []
    blog_unread_count = 0
    try:
        all_messages = load_messages()

        # Unread (for the quirky office-lamp poke icon) — counts replies too
        last_read = load_last_blog_read()
        unread_messages = [m for m in all_messages if str(m.get("timestamp", "")) > last_read]
        blog_unread_count = len(unread_messages)

        # Teaser for main screen: only top-level "posts", sorted newest first, last 3.
        # Enrich with a clean display_date so the UI stays pretty without JS.
        top_level = [m for m in all_messages if not m.get("parent_id")]
        top_level.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
        recent_messages = top_level[:3]

        for m in recent_messages:
            ts = str(m.get("timestamp", ""))
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                m["display_date"] = dt.strftime("%b %-d")
            except Exception:
                m["display_date"] = ts[:10] if ts else "—"
    except Exception:
        recent_messages = []
        blog_unread_count = 0

    return _render("home.html", {
        "request": request,
        "public_mode": PUBLIC_MODE,
        "site_name": SITE_NAME,
        "display_name": DISPLAY_NAME,
        "steps": steps_ctx,
        "heart_rate": hr_ctx,
        "recent_messages": recent_messages,
        "blog_unread_count": blog_unread_count,
    })


@app.get("/xbox", response_class=HTMLResponse)
async def xbox_page(request: Request):
    """Dedicated Xbox profile page with current data and game log."""
    xbox_data = await get_xbox_status()
    raw_log = load_xbox_log()

    # Enrich log for clean, professional display (formatted dates, safe fields)
    formatted_log = []
    for entry in raw_log:
        ts = entry.get("timestamp", "")
        played = ts
        try:
            # Support both naive ISO and Z-suffixed
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            # e.g. "Feb 3, 2026 • 2:14 PM"
            played = dt.strftime("%b %-d, %Y • %-I:%M %p")
        except Exception:
            pass
        formatted_log.append({
            "game": entry.get("game", "—"),
            "timestamp": ts,
            "played": played,
            "device": entry.get("device", "") or "",
        })

    # Add a few display helpers to xbox data for the template (no mutation of cache)
    xbox_display = dict(xbox_data) if xbox_data else {}
    try:
        gs = int(xbox_display.get("gamerscore") or 0)
        xbox_display["gamerscore_display"] = f"{gs:,}"
    except Exception:
        xbox_display["gamerscore_display"] = xbox_display.get("gamerscore") or "0"

    return _render("xbox.html", {
        "request": request,
        "xbox": xbox_display,
        "log": formatted_log,
        "public_mode": PUBLIC_MODE,
        "site_name": SITE_NAME,
        "display_name": DISPLAY_NAME,
    })


@app.get("/home-assistant", response_class=HTMLResponse)
async def ha_page(request: Request):
    """Private Home Assistant control page. Controls require admin token (see blog pattern).
    In PUBLIC_MODE the page renders but shows disabled state and no controls.
    """
    # Fast path: we no longer block the entire page on full HA states for the lighting UI.
    # entity_count is best-effort (falls back to last snapshot or 0). The real lighting data
    # (rooms/groups + live states) is delivered instantly via embedded INITIAL_LIGHTING + background /lights-data.
    states = []
    try:
        if HA_ENABLED:
            # Lightweight: still useful for header count, but do not let a slow/401 HA kill the instant feel.
            states = await get_ha_states()
    except Exception:
        states = []

    # The legacy "groups" + ordered domain list is only used for the disabled-state message path.
    # Keep the computation but it is cheap on the (usually empty or small) states list.
    groups: Dict[str, List[Dict]] = {}
    for s in states:
        eid = s.get("entity_id", "unknown.unknown")
        domain = eid.split(".", 1)[0] if "." in eid else "other"
        if domain in ("sensor", "binary_sensor", "sun", "zone", "device_tracker", "weather"):
            continue
        groups.setdefault(domain, []).append(s)

    for dom in groups:
        groups[dom].sort(key=lambda e: (e.get("attributes", {}).get("friendly_name") or e.get("entity_id")).lower())

    domain_order = ["light", "scene", "switch", "media_player", "input_boolean", "automation", "script", "cover", "fan", "other"]
    ordered_groups = []
    for d in domain_order:
        if d in groups:
            ordered_groups.append((d, groups.pop(d)))
    for d, ents in sorted(groups.items()):
        ordered_groups.append((d, ents))

    # Exact persisted rooms + groups from lighting_config.json — this is what makes the UI feel instant.
    initial_lighting = load_lighting_config() if HA_ENABLED else {"rooms": [], "groups": []}

    # Prefer a fast count from last snapshot when available
    snap_total = (last_ha_lights_snapshot.get("data") or {}).get("total") or 0
    display_count = len(states) or snap_total or (len(initial_lighting.get("rooms", [])) * 2)  # rough but never blocks

    return _render("home-assistant.html", {
        "request": request,
        "groups": ordered_groups,
        "entity_count": display_count,
        "ha_enabled": HA_ENABLED,
        "public_mode": PUBLIC_MODE,
        "has_admin_token": bool(ADMIN_TOKEN),
        "site_name": SITE_NAME,
        "initial_lighting": initial_lighting,
        "display_name": DISPLAY_NAME,
    })


@app.get("/vitals", response_class=HTMLResponse)
async def vitals_dashboard(request: Request, days: int = OURA_DAYS):
    """The Oura Ring vitals dashboard (moved to /vitals per site structure)."""
    # Force clean URLs in public mode — redirect away from any ?days= param
    if PUBLIC_MODE and request.query_params.get("days"):
        return RedirectResponse(url="/vitals", status_code=302)

    # Always use fixed 14-day window in public mode
    if PUBLIC_MODE:
        days = 14
    # Basic rate limiting in public mode
    if PUBLIC_MODE:
        client_ip = request.client.host if request.client else "unknown"
        if not rate_limiter.is_allowed(client_ip):
            return HTMLResponse(
                "<div style='font-family:sans-serif;padding:40px;text-align:center;color:#666'>"
                "Too many requests. Please slow down.</div>",
                status_code=429
            )

    # In public mode we never show the setup screen
    if not OURA_TOKEN or not oura_client:
        if PUBLIC_MODE:
            return HTMLResponse(
                "<h1 style='font-family:sans-serif;color:#111'>Configuration error</h1>"
                "<p>Dashboard is not properly configured.</p>",
                status_code=500
            )
        return _render(
            "dashboard.html",
            {
                "request": request,
                "setup_mode": True,
                "token": OURA_TOKEN,
                "display_name": DISPLAY_NAME,
                "hr_age_minutes": None,
                "recent_activities": [],
            },
        )

    try:
        raw = await fetch_all_data(days)
        ctx = process_dashboard_data(
            raw["personal"],
            raw["readiness"],
            raw["daily_sleep"],
            raw["detailed_sleep"],
            raw["activity"],
            raw["spo2"],
            raw["stress"],
            raw.get("heartrate", []),
            raw.get("workouts", []),
            days,
        )

        ctx.update({
            "request": request,
            "setup_mode": False,
            "error": None,
            "public_mode": PUBLIC_MODE,
            "display_name": DISPLAY_NAME,
            "site_name": SITE_NAME,
            "site_url": SITE_URL,
            "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
        })
        return _render("dashboard.html", ctx)
    except Exception as e:
        if PUBLIC_MODE:
            # In public mode, show a clean error instead of exposing internals
            return _render(
                "dashboard.html",
                {
                    "request": request,
                    "setup_mode": False,
                    "error": "Unable to load latest data right now. Please check back soon.",
                    "public_mode": True,
                    "display_name": DISPLAY_NAME,
                    "site_name": SITE_NAME,
                    "site_url": SITE_URL,
                    "name": DISPLAY_NAME,
                    "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
                    "hr_age_minutes": None,
                    "recent_activities": [],
                    "last_updated_iso": None,
                },
            )
        return _render(
            "dashboard.html",
            {
                "request": request,
                "setup_mode": False,
                "error": str(e),
                "name": "there",
                "display_name": DISPLAY_NAME,
                "hr_age_minutes": None,
                "time_greeting": "HENDERBURGH IS HERE.",
                "recent_activities": [],
                "last_updated_iso": None,
            },
        )


@app.get("/fragment", response_class=HTMLResponse)
async def dashboard_fragment(request: Request, days: int = OURA_DAYS):
    """Legacy fragment endpoint (still works for the vitals dashboard)."""
    return await vitals_fragment(request, days)


@app.get("/vitals/fragment", response_class=HTMLResponse)
async def vitals_fragment(request: Request, days: int = OURA_DAYS):
    # Always ignore ?days= in public mode for clean URLs
    if PUBLIC_MODE:
        days = 14
    """HTMX target — disabled in public mode to protect API quota."""
    if PUBLIC_MODE:
        return HTMLResponse(
            "<div class='text-sm text-zinc-400 p-4'>Data automatically refreshes every ~10 minutes.</div>",
            200
        )

    if not OURA_TOKEN or not oura_client:
        return HTMLResponse("<div class='text-red-400 p-8'>Token not configured</div>", 400)

    try:
        raw = await fetch_all_data(days)
        ctx = process_dashboard_data(
            raw["personal"],
            raw["readiness"],
            raw["daily_sleep"],
            raw["detailed_sleep"],
            raw["activity"],
            raw["spo2"],
            raw["stress"],
            raw.get("heartrate", []),
            raw.get("workouts", []),
            days,
        )
        ctx.update({
            "request": request,
            "setup_mode": False,
            "error": None,
            "fragment": True,
            "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
        })
        return _render("dashboard.html", ctx)
    except Exception as e:
        return HTMLResponse(f"<div class='text-red-400 p-8'>Error: {e}</div>", 500)


@app.get("/api/latest-hr")
async def api_latest_hr(fresh: bool = False):
    """Lightweight endpoint for fast Heart Rate updates.
    Use ?fresh=true for the live modal (bypasses cache for freshest possible data).
    """
    if not oura_client:
        return JSONResponse({"bpm": None, "timestamp": None})

    try:
        now = datetime.now(timezone.utc)

        if fresh:
            # Very recent window for true "live" feel in the modal
            start_dt = (now - timedelta(minutes=30)).isoformat()
            end_dt = now.isoformat()

            hr_data = await oura_client._get(
                "/usercollection/heartrate",
                {"start_datetime": start_dt, "end_datetime": end_dt},
                bypass_cache=True   # Force fresh data from Oura
            )
        else:
            # Normal cached call (used by the regular 2-min background refresh)
            start_dt = (now - timedelta(hours=4)).isoformat()
            end_dt = now.isoformat()

            hr_data = await oura_client._get(
                "/usercollection/heartrate",
                {"start_datetime": start_dt, "end_datetime": end_dt},
                bypass_cache=False
            )

        hr_data = hr_data.get("data", []) if isinstance(hr_data, dict) else []

        latest_hr = None
        latest_hr_timestamp = None

        if hr_data:
            for entry in reversed(hr_data):
                if isinstance(entry, dict) and entry.get("bpm") is not None:
                    latest_hr = entry.get("bpm")
                    latest_hr_timestamp = entry.get("timestamp")
                    break

        if latest_hr is None:
            # Fallback
            recent_start = (now - timedelta(days=1)).date().isoformat()
            detailed = await oura_client.get_sleep(recent_start, now.date().isoformat())
            if detailed:
                latest_detailed = detailed[-1] if detailed else None
                latest_hr = _safe_get(latest_detailed, "average_heart_rate") or _safe_get(latest_detailed, "lowest_heart_rate")

        # Compute age for the response
        age_minutes = None
        if latest_hr_timestamp:
            try:
                ts = datetime.fromisoformat(latest_hr_timestamp.replace("Z", "+00:00"))
                age_minutes = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
            except Exception:
                pass

        return JSONResponse({
            "bpm": int(latest_hr) if latest_hr is not None else None,
            "timestamp": latest_hr_timestamp,
            "age_minutes": age_minutes
        })
    except Exception:
        return JSONResponse({"bpm": None, "timestamp": None})


@app.get("/health")
async def health():
    """Health check endpoint for Railway / monitoring (does not hit Oura API)."""
    return {
        "status": "ok",
        "public_mode": PUBLIC_MODE,
        "has_token": bool(OURA_TOKEN),
        "display_name": DISPLAY_NAME,
        "site": SITE_NAME,
    }


async def get_current_steps():
    """Internal helper to get current steps and miles (same logic as vitals page)."""
    if not oura_client:
        return {"steps": None, "miles": None}

    try:
        today = date.today().isoformat()
        activity = await oura_client.get_daily_activity(today, today)
        if activity and len(activity) > 0:
            latest = activity[0]
            steps = latest.get("steps") or 0
            miles = round(steps * 0.0005, 1) if steps else 0
            return {"steps": steps, "miles": miles}
    except Exception:
        pass
    return {"steps": None, "miles": None}


@app.get("/api/steps")
async def api_steps():
    """Lightweight endpoint for current steps (used on homepage Live Now)."""
    return await get_current_steps()


async def get_current_heart_rate():
    """Internal helper to get current heart rate (same logic used by vitals page)."""
    if not oura_client:
        return {"bpm": None, "updated": None}

    try:
        now = datetime.now(timezone.utc)
        # Get last ~4 hours of HR samples
        start_dt = (now - timedelta(hours=4)).isoformat()
        hr_data = await oura_client._get(
            "/usercollection/heartrate",
            {"start_datetime": start_dt, "end_datetime": now.isoformat()},
            bypass_cache=True
        )
        hr_list = hr_data.get("data", []) if isinstance(hr_data, dict) else []

        if hr_list:
            latest = hr_list[-1]
            bpm = latest.get("bpm")
            ts = latest.get("timestamp")
            return {
                "bpm": bpm,
                "updated": ts
            }
    except Exception:
        pass

    return {"bpm": None, "updated": None}


@app.get("/api/heart-rate")
async def api_heart_rate():
    """Lightweight endpoint for current heart rate (used on homepage Live Now)."""
    return await get_current_heart_rate()


@app.get("/api/xbox/status")
async def get_xbox_status():
    global last_xbox_data, _last_xbox_fetch_time

    # If using placeholder values, return not_configured immediately (no API call)
    # Detection: ONLY trigger on the exact example placeholder strings from .env.example.
    # Real values (including the user's actual key and gamertag="NutNutBiinks") will proceed to call xbl.io.
    # See comment above the XBL_ assignments for full explanation of detection logic.
    if XBL_API_KEY in ("your_real_xbl_key_here", "your_openxbl_api_key_here") or XBL_GAMERTAG in ("your_gamertag_here", "your_exact_gamertag_here"):
        return {"status": "not_configured", "state": "Unknown", "game": "—"}

    # Simple time-based cache: serve last known good data instantly if still fresh.
    # This is the main protection against burning the 150 req/h free tier.
    now = time.time()
    if last_xbox_data.get("status") == "ok":
        age = now - _last_xbox_fetch_time
        if age < XBOX_CACHE_TTL_SECONDS:
            return last_xbox_data

    headers = {
        "X-Authorization": XBL_API_KEY,
        "Accept": "application/json"
    }

    async with httpx.AsyncClient() as client:
        try:
            xuid = XBL_XUID
            if not xuid:
                # Step 1: Resolve gamertag to XUID (only if no direct XUID provided)
                profile_url = f"https://xbl.io/api/v2/player/gamertag/{XBL_GAMERTAG}"
                profile_res = await client.get(profile_url, headers=headers, timeout=10)

                if profile_res.status_code != 200:
                    key_preview = XBL_API_KEY[:8] + "..." if XBL_API_KEY else "None"
                    print(f"Xbox API error (profile): status={profile_res.status_code} body={profile_res.text[:300]} key_preview={key_preview} gamertag={XBL_GAMERTAG}")
                    if profile_res.status_code == 401:
                        print("Xbox: Invalid API key detected - verify your XBL_API_KEY from xbl.io app console (it should return 200 on /account test).")
                    return last_xbox_data

                profile = profile_res.json()
                xuid = profile.get("xuid")

                if not xuid:
                    print("Xbox API error: no xuid returned in profile response")
                    return last_xbox_data

            # Step 2: Get presence using XUID (either from resolution or direct XBL_XUID)
            presence_url = f"https://xbl.io/api/v2/{xuid}/presence"
            presence_res = await client.get(presence_url, headers=headers, timeout=10)

            if presence_res.status_code != 200:
                key_preview = XBL_API_KEY[:8] + "..." if XBL_API_KEY else "None"
                print(f"Xbox API error (presence): status={presence_res.status_code} body={presence_res.text[:300]} key_preview={key_preview} xuid={xuid}")
                return last_xbox_data

            presence = presence_res.json() or {}

            # The xbl.io /presence (and /account) responses wrap the actual data under "content"
            data = presence.get("content") or presence

            state = data.get("state", "Unknown")

            # Step 3: Extract current game/app name - try multiple paths for robustness
            game = "—"

            # Path 1: devices[0].titles (most common for current activity)
            devices = data.get("devices") or []
            if isinstance(devices, list) and len(devices) > 0:
                for device in devices:
                    if not isinstance(device, dict):
                        continue
                    titles = device.get("titles") or []
                    if isinstance(titles, list) and len(titles) > 0:
                        # Prefer Full placement active title (the main game), then any Active
                        for title in titles:
                            if isinstance(title, dict):
                                if title.get("placement") == "Full" and (title.get("state") == "Active" or title.get("placement") == "Full"):
                                    game = title.get("name") or title.get("titleName") or "—"
                                    break
                        if game == "—":
                            for title in titles:
                                if isinstance(title, dict):
                                    if title.get("placement") == "Active" or title.get("state") == "Active":
                                        game = title.get("name") or title.get("titleName") or "—"
                                        break
                        if game == "—":
                            # fallback to first title
                            first_title = titles[0]
                            if isinstance(first_title, dict):
                                game = first_title.get("name") or first_title.get("titleName") or "—"
                    if game != "—":
                        break

            primary_device = ""
            if devices and isinstance(devices, list) and devices:
                d0 = devices[0]
                if isinstance(d0, dict):
                    primary_device = d0.get("type", "")

            # Path 2: direct lastSeenTitle (some response formats)
            if game == "—" and "lastSeenTitle" in data:
                game = data.get("lastSeenTitle") or "—"

            # Path 3: lastSeen.titleName
            if game == "—":
                last_seen = data.get("lastSeen") or {}
                if isinstance(last_seen, dict):
                    game = last_seen.get("titleName") or last_seen.get("name") or "—"

            # Path 4: other possible top-level fields (handle different formats)
            if game == "—":
                game = (
                    data.get("titleName")
                    or data.get("name")
                    or (data.get("title") or {}).get("name") if isinstance(data.get("title"), dict) else data.get("title")
                    or "—"
                )

            # Handle empty / falsy game
            if not game or str(game).strip() == "":
                game = "—"

            # Fetch account profile for gamerpic, gamerscore, tenure (static profile info)
            gamerpic = ""
            gamerscore = 0
            tenure = 0
            gamertag = XBL_GAMERTAG
            real_name = ""
            account_tier = "Gold"
            try:
                account_res = await client.get("https://xbl.io/api/v2/account", headers=headers, timeout=10)
                if account_res.status_code == 200:
                    acc = account_res.json() or {}
                    acc_content = acc.get("content") or acc
                    pus = acc_content.get("profileUsers") or []
                    if pus:
                        pu = pus[0]
                        settings_list = pu.get("settings") or []
                        settings = {s.get("id"): s.get("value") for s in settings_list if isinstance(s, dict)}
                        gamertag = settings.get("Gamertag") or settings.get("ModernGamertag") or gamertag
                        gamerpic = settings.get("GameDisplayPicRaw", "")
                        try:
                            gamerscore = int(settings.get("Gamerscore", 0))
                        except:
                            gamerscore = 0
                        try:
                            tenure = int(settings.get("TenureLevel", 0))
                        except:
                            tenure = 0
                        # Real name (may not be present on all accounts / privacy settings)
                        real_name = (
                            settings.get("RealName")
                            or settings.get("FirstName")
                            or ""
                        )
                        if not real_name:
                            fn = settings.get("FirstName", "") or ""
                            ln = settings.get("LastName", "") or ""
                            real_name = (fn + " " + ln).strip()
                        # Account tier (often Gold / Game Pass etc.)
                        account_tier = settings.get("AccountTier") or settings.get("Tier") or "Gold"
            except Exception as e:
                print(f"Xbox account fetch error: {e}")

            # Log meaningful game changes (only when game actually changes and is valid)
            if game and game != "—":
                try:
                    log = load_xbox_log()
                    last_entry = log[0] if log else None
                    if not last_entry or last_entry.get("game") != game:
                        entry = {
                            "game": game,
                            "timestamp": datetime.now().isoformat(),
                            "device": primary_device or "",
                        }
                        log.insert(0, entry)
                        log = log[:100]  # keep last 100
                        save_xbox_log(log)
                except Exception as e:
                    print(f"Xbox game log error: {e}")

            # Success path: update the shared cache (with timestamp) and return fresh data.
            # Future calls within XBOX_CACHE_TTL_SECONDS will be served from this without touching xbl.io.
            fetch_time = time.time()
            last_xbox_data = {
                "status": "ok",
                "state": state,
                "game": game,
                "gamertag": gamertag,
                "gamerpic": gamerpic,
                "gamerscore": gamerscore,
                "tenure": tenure,
                "real_name": real_name,
                "account_tier": account_tier,
                "xuid": xuid or XBL_XUID,
                "last_updated": fetch_time
            }
            _last_xbox_fetch_time = fetch_time
            return last_xbox_data

        except Exception as e:
            print(f"Xbox API error: {e}")
            return last_xbox_data


# =============================================================================
# Home Assistant API (read for summary/home card; writes protected by admin token)
# =============================================================================

@app.get("/api/ha/summary")
async def api_ha_summary():
    """Lightweight summary used by the homepage Live Now card. Always safe to call."""
    return await get_ha_summary()


@app.get("/api/ha/states")
async def api_ha_states():
    """Full entity list for the /home-assistant control UI (read-only)."""
    if not HA_ENABLED:
        return {"status": "disabled", "entities": []}
    states = await get_ha_states()
    return {"status": "ok", "entities": states}


@app.get("/api/ha/lights-data")
async def api_ha_lights_data():
    """Rich data for the professional lighting dashboard: custom rooms, groups, lights, scenes, unassigned."""
    if not HA_ENABLED:
        return {"status": "disabled"}
    data = await get_ha_lights_data()
    return {"status": "ok", **data}


@app.get("/api/ha/bootstrap")
async def api_ha_bootstrap():
    """Ultra-fast (no HA roundtrip) bootstrap for instant UI.
    Returns exact persisted rooms + Sync Groups from lighting_config.json + last known light states (if any).
    Frontend renders the full structure + skeleton/last-known cards immediately, then background-refreshes live states.
    """
    if not HA_ENABLED:
        return {"status": "disabled", "rooms": [], "groups": [], "last_states": {}}
    struct = get_persisted_lighting_structure()
    return {"status": "ok", **struct}


# --- Lighting management (rooms, groups, assignments) - protected by ADMIN_TOKEN ---

@app.post("/api/ha/lighting/room/create")
async def create_room(token: Optional[str] = None, name: str = ""):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    if not name.strip():
        raise HTTPException(400, "Room name required")
    config = load_lighting_config()
    new_room = {"id": str(uuid.uuid4()), "name": name.strip(), "light_ids": []}
    config["rooms"].append(new_room)
    save_lighting_config(config)
    return {"status": "ok", "room": new_room}


@app.post("/api/ha/lighting/room/rename")
async def rename_room(token: Optional[str] = None, room_id: str = "", name: str = ""):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    config = load_lighting_config()
    for room in config["rooms"]:
        if room["id"] == room_id:
            room["name"] = name.strip()
            save_lighting_config(config)
            return {"status": "ok"}
    raise HTTPException(404, "Room not found")


@app.post("/api/ha/lighting/room/delete")
async def delete_room(token: Optional[str] = None, room_id: str = ""):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    config = load_lighting_config()
    config["rooms"] = [r for r in config["rooms"] if r["id"] != room_id]
    save_lighting_config(config)
    return {"status": "ok"}


@app.post("/api/ha/lighting/assign")
async def assign_light_to_room(token: Optional[str] = None, entity_id: str = "", room_id: str = ""):
    """Assign (move) a light to a room. Pass room_id="" to unassign."""
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    config = load_lighting_config()
    # Remove from all rooms first
    for room in config["rooms"]:
        if entity_id in room.get("light_ids", []):
            room["light_ids"].remove(entity_id)
    if room_id:
        for room in config["rooms"]:
            if room["id"] == room_id:
                if entity_id not in room.get("light_ids", []):
                    room.setdefault("light_ids", []).append(entity_id)
                break
    save_lighting_config(config)
    return {"status": "ok"}


@app.post("/api/ha/lighting/group/create")
async def create_group(token: Optional[str] = None, name: str = ""):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    if not name.strip():
        raise HTTPException(400, "Group name required")
    config = load_lighting_config()
    new_group = {"id": str(uuid.uuid4()), "name": name.strip(), "light_ids": []}
    config["groups"].append(new_group)
    save_lighting_config(config)
    return {"status": "ok", "group": new_group}


@app.post("/api/ha/lighting/group/delete")
async def delete_group(token: Optional[str] = None, group_id: str = ""):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    config = load_lighting_config()
    config["groups"] = [g for g in config["groups"] if g["id"] != group_id]
    save_lighting_config(config)
    return {"status": "ok"}


@app.post("/api/ha/lighting/group/assign")
async def assign_light_to_group(token: Optional[str] = None, entity_id: str = "", group_id: str = "", add: bool = True):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    config = load_lighting_config()
    for group in config["groups"]:
        if group["id"] == group_id:
            if add:
                if entity_id not in group.get("light_ids", []):
                    group.setdefault("light_ids", []).append(entity_id)
            else:
                if entity_id in group.get("light_ids", []):
                    group["light_ids"].remove(entity_id)
            save_lighting_config(config)
            return {"status": "ok"}
    raise HTTPException(404, "Group not found")


@app.post("/api/ha/lighting/group/rename")
async def rename_group(token: Optional[str] = None, group_id: str = "", name: str = ""):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    config = load_lighting_config()
    for group in config.get("groups", []):
        if group["id"] == group_id:
            group["name"] = name.strip()
            save_lighting_config(config)
            return {"status": "ok"}
    raise HTTPException(404, "Group not found")


@app.post("/api/ha/service/{domain}/{service}")
async def api_ha_service(
    domain: str,
    service: str,
    token: Optional[str] = None,
    payload: dict = Body(default={}),
):
    """Generic service caller with rich payload support.
    Requires ?token= matching ADMIN_TOKEN.
    payload can include: entity_id, brightness (0-255), color_temp (mireds), rgb_color: [r,g,b], etc.
    Used by the professional lighting dashboard.
    """
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    if PUBLIC_MODE or not HA_ENABLED:
        raise HTTPException(status_code=403, detail="Home Assistant controls are disabled in public mode")

    entity_id = payload.get("entity_id")
    extra = {k: v for k, v in payload.items() if k != "entity_id"}

    result = await call_ha_service(domain, service, entity_id, extra)
    return {"status": "ok", "domain": domain, "service": service, "entity_id": entity_id, "result": result}


@app.post("/api/ha/poke")
async def api_ha_poke(
    token: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
):
    """Poke action: instantaneous blink (off ~0.25s then back on). Backend handled.
    Rate limited server-side + client. Returns instantly (bg task does the sequence).
    """
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    if PUBLIC_MODE or not HA_ENABLED:
        raise HTTPException(status_code=403, detail="Home Assistant controls are disabled in public mode")

    global _poke_last
    now = time.time()
    if now - _poke_last < 3.0:
        raise HTTPException(status_code=429, detail="Rate limited. Wait 3 seconds between pokes.")
    _poke_last = now

    if background_tasks is not None:
        background_tasks.add_task(perform_poke_blink)
    else:
        asyncio.create_task(perform_poke_blink())
    return {"status": "ok", "action": "poke"}


@app.post("/api/ha/notify")
async def api_ha_notify(
    token: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
):
    """Notification action: temporarily blue for a few seconds, then restore.
    Backend handled reliably. Returns instantly.
    """
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    if PUBLIC_MODE or not HA_ENABLED:
        raise HTTPException(status_code=403, detail="Home Assistant controls are disabled in public mode")

    global _notify_last
    now = time.time()
    if now - _notify_last < 5.0:
        raise HTTPException(status_code=429, detail="Rate limited.")
    _notify_last = now

    if background_tasks is not None:
        background_tasks.add_task(perform_notify_blue)
    else:
        asyncio.create_task(perform_notify_blue())
    return {"status": "ok", "action": "notify"}


@app.post("/api/ha/access-request")
async def post_access_request(request: Request, name: str = Form(""), message: str = Form("")):
    """Anyone can submit an access request when the lighting page is locked.
    No admin token required. Logs basic info + user message.
    """
    if not message or not message.strip():
        raise HTTPException(400, "Message is required")

    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    ts = datetime.now(timezone.utc).isoformat()

    req = {
        "id": str(uuid.uuid4())[:8],
        "name": (name or "Anonymous").strip(),
        "message": message.strip(),
        "ip": ip,
        "user_agent": ua,
        "timestamp": ts
    }

    reqs = load_access_requests()
    reqs.insert(0, req)  # newest first
    save_access_requests(reqs)
    return {"status": "received"}


@app.get("/api/ha/access-requests")
async def get_access_requests(token: Optional[str] = None):
    """Protected: list access requests (only when page is unlocked with valid token)."""
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return load_access_requests()


@app.delete("/api/ha/access-requests/{req_id}")
async def delete_access_request(req_id: str, token: Optional[str] = None):
    """Protected: delete a specific request."""
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    reqs = load_access_requests()
    reqs = [r for r in reqs if r.get("id") != req_id]
    save_access_requests(reqs)
    return {"status": "deleted"}


# =============================================================================
# Xbox Game Log (persistent recently played)
# =============================================================================
XBOX_LOG_FILE = Path("data/xbox_log.json")
XBOX_LOG_FILE.parent.mkdir(exist_ok=True)

def load_xbox_log():
    if XBOX_LOG_FILE.exists():
        try:
            return json.loads(XBOX_LOG_FILE.read_text())
        except Exception:
            pass
    return []

def save_xbox_log(log):
    XBOX_LOG_FILE.write_text(json.dumps(log, indent=2))


# =============================================================================
# Home Assistant Lighting Config (custom rooms + groups, persistent)
# =============================================================================
LIGHTING_CONFIG_FILE = Path("data/lighting_config.json")
LIGHTING_CONFIG_FILE.parent.mkdir(exist_ok=True)

DEFAULT_LIGHTING_CONFIG = {
    "rooms": [],   # list of {"id": str, "name": str, "light_ids": list[str]}
    "groups": []   # same structure, used as Sync Groups / Pairs
}

def load_lighting_config():
    if LIGHTING_CONFIG_FILE.exists():
        try:
            config = json.loads(LIGHTING_CONFIG_FILE.read_text())
            if "rooms" not in config:
                config["rooms"] = []
            if "groups" not in config:
                config["groups"] = []
            return config
        except Exception:
            pass
    return DEFAULT_LIGHTING_CONFIG.copy()

def save_lighting_config(config):
    LIGHTING_CONFIG_FILE.write_text(json.dumps(config, indent=2))

def get_persisted_lighting_structure() -> Dict[str, Any]:
    """Fast, no-HA call. Returns rooms + groups exactly as persisted + any last known light states.
    Used by /api/ha/bootstrap for instant UI render of structure before live HA data arrives.
    """
    config = load_lighting_config()
    snap = last_ha_lights_snapshot.get("data") or {}
    return {
        "rooms": config.get("rooms", []),
        "groups": config.get("groups", []),
        "last_states": snap.get("lights", {}),
        "total": snap.get("total", 0),
        "ts": last_ha_lights_snapshot.get("ts", 0),
    }


# =============================================================================
# Access Requests (for locked lighting page - simple request logging)
# =============================================================================
ACCESS_REQUESTS_FILE = Path("data/access_requests.json")
ACCESS_REQUESTS_FILE.parent.mkdir(exist_ok=True)

def load_access_requests():
    if ACCESS_REQUESTS_FILE.exists():
        try:
            return json.loads(ACCESS_REQUESTS_FILE.read_text())
        except Exception:
            pass
    return []

def save_access_requests(reqs):
    ACCESS_REQUESTS_FILE.write_text(json.dumps(reqs, indent=2))


# =============================================================================
# Home Assistant helpers
# =============================================================================

async def get_ha_states():
    """Fetch all entity state from Home Assistant. Returns [] if disabled or error."""
    if not HA_ENABLED:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{HA_URL}/api/states", headers=HA_HEADERS)
            resp.raise_for_status()
            return resp.json() or []
    except Exception as e:
        print(f"HA states fetch error: {e}")
        return []


async def get_ha_areas():
    """Fetch areas (rooms) from HA config."""
    if not HA_ENABLED:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{HA_URL}/api/config/areas", headers=HA_HEADERS)
            resp.raise_for_status()
            return resp.json() or []
    except Exception as e:
        print(f"HA areas fetch error: {e}")
        return []


async def get_ha_entity_registry():
    """Fetch entity registry for area assignments."""
    if not HA_ENABLED:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{HA_URL}/api/config/entity_registry", headers=HA_HEADERS)
            resp.raise_for_status()
            return resp.json() or []
    except Exception as e:
        print(f"HA entity registry fetch error: {e}")
        return []


async def get_ha_summary():
    """Improved summary for homepage Live Now card with room-level status.
    Uses the same area logic as the dashboard for consistency.
    """
    if not HA_ENABLED:
        return {
            "status": "disabled",
            "lights_on": 0,
            "total_lights": 0,
            "active_scene": "—",
            "message": "Home Assistant available in local/private mode only."
        }

    data = await get_ha_lights_data()
    rooms = data.get("rooms", [])
    scenes = data.get("scenes", [])

    total_lights = data.get("total_lights", 0)
    lights_on = 0
    room_status = []

    for room in rooms:
        on = room.get("on_count", 0)
        total = room.get("light_count", 0)
        lights_on += on
        if total:
            room_status.append({
                "name": room.get("name"),
                "on": on,
                "total": total,
                "brightness_avg": None,  # could enhance later
            })

    # Best active scene guess
    active_scene = None
    for sc in scenes:
        if sc.get("state") in ("on", "scening"):
            active_scene = sc.get("friendly_name")
            break
    if not active_scene and scenes:
        # pick first as "favorite" hint, but prefer "—"
        active_scene = None

    # Top active rooms for the card
    active_rooms = sorted(
        [r for r in room_status if r["on"] > 0],
        key=lambda r: (r["on"], r["total"]),
        reverse=True
    )[:3]

    return {
        "status": "ok",
        "lights_on": lights_on,
        "total_lights": total_lights,
        "active_scene": active_scene or "—",
        "rooms": room_status,
        "active_rooms": active_rooms,
        "last_updated": time.time(),
    }


def _avg_brightness(lights: List[Dict]) -> Optional[int]:
    vals = []
    for l in lights:
        b = l.get("attributes", {}).get("brightness")
        if b is not None:
            vals.append(int(b))
    if not vals:
        return None
    return int(sum(vals) / len(vals) / 255 * 100)  # percent


async def call_ha_service(domain: str, service: str, entity_id: Optional[str] = None, extra: Optional[dict] = None):
    """Call a Home Assistant service. Internal; protection done at route level.
    extra can contain brightness, color_temp, rgb_color etc.
    """
    if not HA_ENABLED:
        raise HTTPException(status_code=503, detail="Home Assistant not enabled (public mode or no token)")
    payload: dict = extra.copy() if extra else {}
    if entity_id:
        payload.setdefault("entity_id", entity_id)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            url = f"{HA_URL}/api/services/{domain}/{service}"
            resp = await client.post(url, headers=HA_HEADERS, json=payload)
            if resp.status_code >= 400:
                print(f"HA service error {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception:
                return {"status": "called"}
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"HA error: {e.response.text[:200] if e.response else str(e)}")
    except Exception as e:
        print(f"HA service call failed: {domain}/{service} {entity_id} - {e}")
        raise HTTPException(status_code=502, detail=f"Failed to reach Home Assistant: {str(e)}")


async def perform_poke_blink():
    """Backend-handled snappy poke: instant off for 0.25s then back on to previous state.
    Runs in background so endpoint returns immediately for instant feel.
    """
    global _poke_last
    try:
        states = await get_ha_states()
        prev = None
        for s in states:
            if s.get("entity_id") == OFFICE_LAMP:
                attrs = s.get("attributes", {}) or {}
                prev = {
                    "state": s.get("state"),
                    "brightness": attrs.get("brightness"),
                    "color_temp": attrs.get("color_temp"),
                    "rgb_color": attrs.get("rgb_color"),
                }
                break

        # instantaneous off
        await call_ha_service("light", "turn_off", OFFICE_LAMP, {"transition": 0})
        await asyncio.sleep(0.25)

        # back on to previous (or on if no prev)
        if prev and prev.get("state") == "off":
            await call_ha_service("light", "turn_off", OFFICE_LAMP, {"transition": 0})
        else:
            extra = {"transition": 0}
            if prev:
                if prev.get("brightness") is not None:
                    extra["brightness"] = prev["brightness"]
                if prev.get("color_temp") is not None:
                    extra["color_temp"] = prev["color_temp"]
                if prev.get("rgb_color"):
                    extra["rgb_color"] = prev["rgb_color"]
            await call_ha_service("light", "turn_on", OFFICE_LAMP, extra)
    except Exception as e:
        print(f"perform_poke_blink error: {e}")


async def perform_notify_blue():
    """Backend-handled notification: temp set to blue for ~3.5s then restore previous.
    Runs in background.
    """
    global _notify_last
    try:
        states = await get_ha_states()
        prev = None
        for s in states:
            if s.get("entity_id") == OFFICE_LAMP:
                attrs = s.get("attributes", {}) or {}
                prev = {
                    "state": s.get("state"),
                    "brightness": attrs.get("brightness"),
                    "color_temp": attrs.get("color_temp"),
                    "rgb_color": attrs.get("rgb_color"),
                }
                break

        # set blue instantly
        blue = {
            "brightness": 200,
            "rgb_color": [0, 80, 255],
            "transition": 0,
        }
        await call_ha_service("light", "turn_on", OFFICE_LAMP, blue)
        await asyncio.sleep(3.5)

        # restore
        if prev:
            if prev.get("state") == "off":
                await call_ha_service("light", "turn_off", OFFICE_LAMP, {"transition": 0.4})
            else:
                extra = {"transition": 0.4}
                if prev.get("brightness") is not None:
                    extra["brightness"] = prev["brightness"]
                if prev.get("color_temp") is not None:
                    extra["color_temp"] = prev["color_temp"]
                if prev.get("rgb_color"):
                    extra["rgb_color"] = prev["rgb_color"]
                await call_ha_service("light", "turn_on", OFFICE_LAMP, extra)
    except Exception as e:
        print(f"perform_notify_blue error: {e}")


async def get_ha_lights_data():
    """Rich data for lighting dashboard.
    Uses HA for discovering lights + effects.
    Uses exact rooms/groups from lighting_config.json for organization (user can create, move lights, rename, etc.).
    No auto-seeding - config.json is the source of truth for room assignments.
    New/unmatched lights go to Unassigned.
    Updates the last_ha_lights_snapshot so bootstrap + "last known" are instant on next loads.
    """
    if not HA_ENABLED:
        return {"rooms": [], "groups": [], "lights_by_room": {}, "unassigned_lights": [], "scenes": [], "total_lights": 0}

    states = await get_ha_states()
    areas_raw = await get_ha_areas()
    registry = await get_ha_entity_registry()

    # Build HA area name map
    ha_area_names = {a.get("area_id"): a.get("name", "Unknown") for a in areas_raw}

    # entity -> ha_area_name fallback
    entity_to_ha_area = {}
    for entry in registry:
        eid = entry.get("entity_id")
        aid = entry.get("area_id")
        if eid and aid and aid in ha_area_names:
            entity_to_ha_area[eid] = ha_area_names[aid]

    config = load_lighting_config()
    rooms = config.get("rooms", [])
    groups = config.get("groups", [])

    # Build set of all custom room light ids for unassigned detection
    # NO auto-seed logic - use exact mapping from config.json only
    assigned_light_ids = set()
    for room in rooms:
        assigned_light_ids.update(room.get("light_ids", []))

    lights_by_room: Dict[str, List[Dict]] = {}
    unassigned_lights = []
    all_lights = []
    sync_controls = []  # Wiz Sync Box or similar - surfaced so you can control the box itself (often appears as light.* or select.*)

    for s in states:
        eid = str(s.get("entity_id", ""))
        attrs = s.get("attributes", {}) or {}
        friendly = attrs.get("friendly_name") or eid.split(".", 1)[1].replace("_", " ").title()

        is_light = eid.startswith("light.")
        is_sync_related = any(k in eid.lower() for k in ("sync", "wiz_sync", "syncbox", "sync_box")) or "sync" in friendly.lower()

        if not (is_light or is_sync_related):
            continue

        # Build unified entry
        entry = {
            "entity_id": eid,
            "state": s.get("state", "off"),
            "attributes": attrs,
            "friendly_name": friendly,
            "supported_color_modes": attrs.get("supported_color_modes", []),
            "effect_list": attrs.get("effect_list", []),
            "brightness": attrs.get("brightness"),
            "color_temp": attrs.get("color_temp"),
            "rgb_color": attrs.get("rgb_color"),
            "current_effect": attrs.get("effect"),
            "room_name": "Unassigned",
            "is_sync": bool(is_sync_related),
            "options": attrs.get("options", []),  # for select.* sync modes (Vivid, Theater, etc.) if the entity exposes them
        }

        # Sync box / non-plain-light sync controls get their own list (so UI can highlight them)
        if is_sync_related and not is_light:
            sync_controls.append(entry)
            # Still try to classify into a room below if user manually assigned the entity id in config
            # (rare but supported)
        else:
            all_lights.append(entry)

        # Find which custom room this belongs to (by exact id in lighting_config.json) - strictly from config
        room_name = "Unassigned"
        for room in rooms:
            if eid in room.get("light_ids", []):
                room_name = room["name"]
                break

        # Update entry room for display
        entry["room_name"] = room_name

        if is_sync_related and not is_light:
            # already added to sync_controls; if it was assigned to a room we still want it in lights_by_room too for the room view
            if room_name != "Unassigned":
                lights_by_room.setdefault(room_name, []).append(entry)
            else:
                # put sync boxes that aren't assigned into a visible "sync" bucket in unassigned for now
                # (frontend will show is_sync badge)
                pass
        else:
            if room_name == "Unassigned":
                unassigned_lights.append(entry)
            else:
                lights_by_room.setdefault(room_name, []).append(entry)

    # Sort lights in each room
    for rname in lights_by_room:
        lights_by_room[rname].sort(key=lambda x: x["friendly_name"].lower())
    unassigned_lights.sort(key=lambda x: x["friendly_name"].lower())

    # Scenes
    scenes = []
    for s in states:
        if str(s.get("entity_id", "")).startswith("scene."):
            attrs = s.get("attributes", {}) or {}
            scenes.append({
                "entity_id": s["entity_id"],
                "state": s.get("state"),
                "friendly_name": attrs.get("friendly_name") or s["entity_id"].split(".", 1)[1].replace("_", " ").title(),
                "description": attrs.get("description") or ""
            })
    scenes.sort(key=lambda x: x["friendly_name"].lower())

    # Prepare rooms list for UI (include counts)
    rooms_for_ui = []
    for room in rooms:
        rlights = [l for l in all_lights if l["entity_id"] in room.get("light_ids", [])]
        rooms_for_ui.append({
            "id": room["id"],
            "name": room["name"],
            "light_ids": room.get("light_ids", []),
            "light_count": len(rlights),
            "on_count": sum(1 for l in rlights if l["state"] == "on")
        })

    groups_for_ui = []
    for group in groups:
        glights = [l for l in all_lights if l["entity_id"] in group.get("light_ids", [])]
        groups_for_ui.append({
            "id": group["id"],
            "name": group["name"],
            "light_ids": group.get("light_ids", []),
            "light_count": len(glights),
            "on_count": sum(1 for l in glights if l["state"] == "on")
        })

    result = {
        "rooms": rooms_for_ui,
        "groups": groups_for_ui,
        "lights_by_room": lights_by_room,   # only lights assigned to custom rooms
        "unassigned_lights": unassigned_lights,
        "scenes": scenes,
        "total_lights": len(all_lights),
        "sync_controls": sync_controls,  # Wiz Sync Box etc. - shown in UI as special items you can control directly
    }

    # Update in-memory last known snapshot for instant bootstrap + "last known states" on next visit / refresh
    global last_ha_lights_snapshot
    lights_map = {}
    for l in all_lights:
        lights_map[l["entity_id"]] = {
            "state": l["state"],
            "brightness": l.get("brightness"),
            "current_effect": l.get("current_effect"),
            "color_temp": l.get("color_temp"),
            "rgb_color": l.get("rgb_color"),
        }
    last_ha_lights_snapshot = {
        "data": {"lights": lights_map, "total": len(all_lights)},
        "ts": time.time()
    }

    return result


# =============================================================================
# Golf Club Distances (server-persisted, shared across visitors)
# =============================================================================
CLUBS_FILE = Path("data/clubs.json")
CLUBS_FILE.parent.mkdir(exist_ok=True)

DEFAULT_CLUBS = [
    {"club": "Driver", "yards": 245},
    {"club": "3 Wood", "yards": 220},
    {"club": "5 Wood", "yards": 200},
    {"club": "4 Iron", "yards": 185},
    {"club": "5 Iron", "yards": 175},
    {"club": "6 Iron", "yards": 165},
    {"club": "7 Iron", "yards": 155},
    {"club": "8 Iron", "yards": 145},
    {"club": "9 Iron", "yards": 135},
    {"club": "PW", "yards": 125},
    {"club": "GW", "yards": 110},
    {"club": "SW", "yards": 90},
]

def load_clubs():
    if CLUBS_FILE.exists():
        try:
            return json.loads(CLUBS_FILE.read_text())
        except Exception:
            pass
    return DEFAULT_CLUBS.copy()

def save_clubs(clubs):
    CLUBS_FILE.write_text(json.dumps(clubs, indent=2))


# =============================================================================
# Blog Message Board (threaded, server-persisted)
# =============================================================================

MESSAGES_FILE = Path("data/messages.json")
MESSAGES_FILE.parent.mkdir(exist_ok=True)

LAST_BLOG_READ_FILE = Path("data/last_blog_read.json")

def load_messages():
    if MESSAGES_FILE.exists():
        try:
            return json.loads(MESSAGES_FILE.read_text())
        except Exception:
            pass
    return []

def save_messages(messages):
    MESSAGES_FILE.write_text(json.dumps(messages, indent=2))

def load_last_blog_read():
    if LAST_BLOG_READ_FILE.exists():
        try:
            data = json.loads(LAST_BLOG_READ_FILE.read_text())
            return data.get("timestamp", "1970-01-01T00:00:00")
        except Exception:
            pass
    return "1970-01-01T00:00:00"

def save_last_blog_read(timestamp=None):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    LAST_BLOG_READ_FILE.write_text(json.dumps({"timestamp": timestamp}))

@app.get("/api/messages")
async def get_messages():
    messages = load_messages()
    # Sort newest first
    messages.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
    return messages

@app.post("/api/messages")
async def post_message(message: dict):
    messages = load_messages()
    new_message = {
        "id": str(datetime.now().timestamp()),
        "name": message.get("name", "Anonymous"),
        "text": message.get("text", ""),
        "parent_id": message.get("parent_id"),  # None for top-level messages
        "timestamp": datetime.now().isoformat()
    }
    messages.append(new_message)
    save_messages(messages)
    return new_message


@app.delete("/api/messages/{message_id}")
async def delete_message(message_id: str, token: str = None):
    """Delete a message. Requires admin token via query param ?token=..."""
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")

    messages = load_messages()
    original_len = len(messages)
    messages = [m for m in messages if m.get("id") != message_id]

    if len(messages) == original_len:
        raise HTTPException(status_code=404, detail="Message not found")

    save_messages(messages)
    return {"status": "deleted"}

@app.post("/api/blog/mark-read")
async def mark_blog_read():
    """Mark all current blog messages as read (for the smart notification hub on home). Personal use, no token needed."""
    save_last_blog_read()
    return {"status": "marked"}


# =============================================================================
# Simple placeholder routes for future sections (Golf, Clips, Blog)
# =============================================================================

def _placeholder_page(title: str, description: str = "") -> HTMLResponse:
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} • HENDERBURGH</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body {{ font-family: 'Inter', system_ui, sans-serif; }}
  </style>
</head>
<body class="bg-white text-[#111] min-h-screen flex items-center justify-center">
  <div class="max-w-md mx-auto px-6 text-center">
    <a href="/" class="inline-flex items-center gap-x-2 text-sm text-neutral-500 hover:text-neutral-700 mb-8">
      <span>←</span> <span>HENDERBURGH</span>
    </a>
    <h1 class="text-4xl font-semibold tracking-tight mb-3">{title}</h1>
    <p class="text-lg text-neutral-600 mb-8">{description or "This section is coming soon."}</p>
    <a href="/" class="inline-block px-6 py-2.5 text-sm border border-neutral-200 rounded-2xl hover:bg-neutral-50 transition">Back to home</a>
    <div class="mt-12 text-[10px] text-neutral-400">HENDERBURGH</div>
  </div>
</body>
</html>
"""
    return HTMLResponse(html)


@app.get("/api/golf/clubs")
async def get_clubs():
    return load_clubs()

@app.post("/api/golf/clubs")
async def update_clubs(clubs: List[dict]):
    save_clubs(clubs)
    return {"status": "saved"}


@app.get("/golf", response_class=HTMLResponse)
async def golf_page():
    return _render("golf.html", {
        "request": None,
    })


@app.get("/clips", response_class=HTMLResponse)
async def clips_page():
    return _render("clips.html", {
        "request": None,
    })


@app.get("/blog", response_class=HTMLResponse)
async def blog_page():
    return _render("blog.html", {
        "request": None,
        "public_mode": PUBLIC_MODE,
        "site_name": SITE_NAME,
        "display_name": DISPLAY_NAME,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
