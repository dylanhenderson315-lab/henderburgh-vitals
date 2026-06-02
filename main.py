"""
HENDER VITALS Dashboard
Beautiful personal Oura Ring dashboard — FastAPI + HTMX + Tailwind + Chart.js
Run with: uvicorn main:app --reload
"""

from __future__ import annotations

import os
import asyncio
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
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
AUTO_REFRESH_SECONDS = int(os.getenv("AUTO_REFRESH_SECONDS", "180" if PUBLIC_MODE else "0"))  # 3 min default in public for fresher data

# Admin token for protected actions (e.g. deleting blog messages)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# Xbox (OpenXBL) configuration for Live Now section
# XBL_API_KEY and XBL_GAMERTAG must be set in Railway (production) or .env (local) for this to work.
# See .env.example for setup instructions.
XBL_API_KEY = os.getenv("XBL_API_KEY", "7ede4621-fd2d-4928-919e-8f520a85804d")
XBL_GAMERTAG = os.getenv("XBL_GAMERTAG", "NutNutBiinks")

# Warn at startup if still using the placeholder Xbox key (common cause of "unavailable")
if (XBL_API_KEY in ("7ede4621-fd2d-4928-919e-8f520a85804d", "your_real_xbl_key_here", "your_openxbl_api_key_here")
        or XBL_GAMERTAG in ("NutNutBiinks", "your_gamertag_here", "your_exact_gamertag_here")):
    print("⚠️  WARNING: Using placeholder XBL_API_KEY / XBL_GAMERTAG. Xbox status will be unavailable until you set real values in .env or Railway env vars. See .env.example for instructions.")

# Admin token used for protected actions (e.g. deleting messages on /blog)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# In-memory cache for last successful Xbox status (survives between requests in the same process)
# Simple caching + fallback to last known good status
last_xbox_data = {"status": "unavailable", "state": "Unknown", "game": "—"}

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

    # Load real recent messages from the message board for the blog teaser
    try:
        all_messages = load_messages()
        recent_messages = all_messages[:3]  # Show latest 3
    except Exception:
        recent_messages = []

    return _render("home.html", {
        "request": request,
        "public_mode": PUBLIC_MODE,
        "site_name": SITE_NAME,
        "display_name": DISPLAY_NAME,
        "steps": steps_ctx,
        "heart_rate": hr_ctx,
        "recent_messages": recent_messages,
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
    global last_xbox_data

    # If using placeholder values, return not_configured immediately (no API call)
    placeholder_keys = ("7ede4621-fd2d-4928-919e-8f520a85804d", "your_real_xbl_key_here", "your_openxbl_api_key_here")
    placeholder_gts = ("NutNutBiinks", "your_gamertag_here", "your_exact_gamertag_here")
    if XBL_API_KEY in placeholder_keys or XBL_GAMERTAG in placeholder_gts:
        return {"status": "not_configured", "state": "Unknown", "game": "—"}

    headers = {
        "X-Authorization": XBL_API_KEY,
        "Accept": "application/json"
    }

    async with httpx.AsyncClient() as client:
        try:
            # Step 1: Resolve gamertag to XUID
            profile_url = f"https://xbl.io/api/v2/player/gamertag/{XBL_GAMERTAG}"
            profile_res = await client.get(profile_url, headers=headers, timeout=10)

            if profile_res.status_code != 200:
                print(f"Xbox API error (profile): status={profile_res.status_code} body={profile_res.text[:300]}")
                return last_xbox_data

            profile = profile_res.json()
            xuid = profile.get("xuid")

            if not xuid:
                print("Xbox API error: no xuid returned in profile response")
                return last_xbox_data

            # Step 2: Get presence
            presence_url = f"https://xbl.io/api/v2/{xuid}/presence"
            presence_res = await client.get(presence_url, headers=headers, timeout=10)

            if presence_res.status_code != 200:
                print(f"Xbox API error (presence): status={presence_res.status_code} body={presence_res.text[:300]}")
                return last_xbox_data

            presence = presence_res.json() or {}

            state = presence.get("state", "Unknown")

            # Step 3: Extract current game/app name - try multiple paths for robustness
            game = "—"

            # Path 1: devices[0].titles (most common for current activity)
            devices = presence.get("devices") or []
            if isinstance(devices, list) and len(devices) > 0:
                for device in devices:
                    if not isinstance(device, dict):
                        continue
                    titles = device.get("titles") or []
                    if isinstance(titles, list) and len(titles) > 0:
                        # Prefer active title if present
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

            # Path 2: direct lastSeenTitle (some response formats)
            if game == "—" and "lastSeenTitle" in presence:
                game = presence.get("lastSeenTitle") or "—"

            # Path 3: lastSeen.titleName
            if game == "—":
                last_seen = presence.get("lastSeen") or {}
                if isinstance(last_seen, dict):
                    game = last_seen.get("titleName") or last_seen.get("name") or "—"

            # Path 4: other possible top-level fields (handle different formats)
            if game == "—":
                game = (
                    presence.get("titleName")
                    or presence.get("name")
                    or (presence.get("title") or {}).get("name") if isinstance(presence.get("title"), dict) else presence.get("title")
                    or "—"
                )

            # Handle empty / falsy game
            if not game or str(game).strip() == "":
                game = "—"

            # Only update cache and return success if we actually got something
            last_xbox_data = {
                "status": "ok",
                "state": state,
                "game": game
            }
            return last_xbox_data

        except Exception as e:
            print(f"Xbox API error: {e}")
            return last_xbox_data


# =============================================================================
# Golf Club Distances (server-persisted, shared across visitors)
# =============================================================================
import json
from pathlib import Path

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

def load_messages():
    if MESSAGES_FILE.exists():
        try:
            return json.loads(MESSAGES_FILE.read_text())
        except Exception:
            pass
    return []

def save_messages(messages):
    MESSAGES_FILE.write_text(json.dumps(messages, indent=2))

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
