"""
Oura Vitals Dashboard
Beautiful personal Oura Ring dashboard — FastAPI + HTMX + Tailwind + Chart.js
Run with: uvicorn main:app --reload
"""

from __future__ import annotations

import os
import asyncio
import time
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
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
DISPLAY_NAME = os.getenv("DISPLAY_NAME", "Dylan").strip() or "Dylan"
SITE_NAME = os.getenv("SITE_NAME", "Henderburgh")
SITE_URL = os.getenv("SITE_URL", "https://henderburgh.com")

# Longer cache in public mode to protect Oura API quota
DEFAULT_CACHE_TTL = 600 if PUBLIC_MODE else 180  # 10 min public, 3 min local
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL))

# Railway (and other platforms) inject PORT. Default to 8000 for local dev.
PORT = int(os.getenv("PORT", 8000))

# Auto-refresh interval in seconds (0 or negative = disabled)
AUTO_REFRESH_SECONDS = int(os.getenv("AUTO_REFRESH_SECONDS", "300" if PUBLIC_MODE else "0"))

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
# Vape (Fogger) Logging - Simple SQLite
# =============================================================================
DB_PATH = os.getenv("DB_PATH", "vape_log.db")

def init_vape_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vape_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            notes TEXT
        )
    """)
    conn.commit()
    conn.close()
    print("✅ Vape database initialized")

def log_vape_session(notes: Optional[str] = None):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO vape_sessions (timestamp, notes) VALUES (?, ?)",
        (now, notes)
    )
    conn.commit()
    conn.close()

def get_vape_stats(days: int = 30) -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Total in last N days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cursor.execute(
        "SELECT COUNT(*) FROM vape_sessions WHERE timestamp >= ?",
        (cutoff,)
    )
    total = cursor.fetchone()[0]

    # Today
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    cursor.execute(
        "SELECT COUNT(*) FROM vape_sessions WHERE timestamp >= ?",
        (today_start,)
    )
    today = cursor.fetchone()[0]

    # This week (last 7 days)
    week_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    cursor.execute(
        "SELECT COUNT(*) FROM vape_sessions WHERE timestamp >= ?",
        (week_start,)
    )
    week = cursor.fetchone()[0]

    # Last hit
    cursor.execute(
        "SELECT timestamp FROM vape_sessions ORDER BY timestamp DESC LIMIT 1"
    )
    last_row = cursor.fetchone()
    last_hit = last_row[0] if last_row else None

    conn.close()

    return {
        "total_last_30d": total,
        "today": today,
        "last_7d": week,
        "last_hit": last_hit,
    }

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

    async def _get(self, path: str, params: Optional[Dict] = None, bypass_cache: bool = False) -> Dict[str, Any]:
        params = params or {}
        key = self._cache_key(path, params)
        now = asyncio.get_event_loop().time()

        if not bypass_cache and key in self._cache:
            ts, data = self._cache[key]
            if now - ts < CACHE_TTL_SECONDS:
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
        """Fetch recent heart rate readings. Note: Oura uses datetime params for this endpoint."""
        try:
            data = await self._get(
                "/usercollection/heartrate", 
                {"start_date": start, "end_date": end},
                bypass_cache=bypass_cache
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

    # Name
    name = personal.get("name") or personal.get("email", "there").split("@")[0].title()

    now = datetime.now(ZoneInfo("UTC"))
    hour = now.hour
    if hour < 5:
        greeting = "NIGHT"
    elif hour < 12:
        greeting = "MORNING"
    elif hour < 17:
        greeting = "AFTERNOON"
    else:
        greeting = "EVENING"

    return {
        "name": name,
        "last_updated": now.strftime("%b %d, %H:%M UTC"),
        "now": now,
        "greeting": greeting,
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

        # Sleep breakdown
        "total_sleep": _fmt_duration(total_sleep),
        "deep_sleep": _fmt_duration(deep),
        "rem_sleep": _fmt_duration(rem),
        "light_sleep": _fmt_duration(light),
        "sleep_efficiency": sleep_efficiency,

        # Activity
        "steps": steps,
        "active_calories": active_cal,

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
app = FastAPI(title="Oura Vitals", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="templates")

# Global client (simple singleton for local dashboard)
oura_client: Optional[OuraClient] = None


@app.on_event("startup")
async def startup():
    global oura_client
    init_vape_db()
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

    personal, readiness, daily_sleep, detailed_sleep, activity, spo2, stress, heartrate = await asyncio.gather(
        oura_client.get_personal_info(),
        oura_client.get_daily_readiness(start, end),
        oura_client.get_daily_sleep(start, end),
        oura_client.get_sleep(start, end),
        oura_client.get_daily_activity(start, end),
        oura_client.get_daily_spo2(start, end),
        oura_client.get_daily_stress(start, end),
        oura_client.get_heartrate(start, end),
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
async def dashboard(request: Request, days: int = OURA_DAYS):
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
                "vape_stats": {"today": 0, "last_7d": 0, "last_hit": None},
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
            days,
        )
        vape_stats = get_vape_stats()

        ctx.update({
            "request": request,
            "setup_mode": False,
            "error": None,
            "public_mode": PUBLIC_MODE,
            "display_name": DISPLAY_NAME,
            "site_name": SITE_NAME,
            "site_url": SITE_URL,
            "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
            "vape_stats": vape_stats,
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
                    "vape_stats": {"today": 0, "last_7d": 0, "last_hit": None},
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
                "vape_stats": {"today": 0, "last_7d": 0, "last_hit": None},
            },
        )


@app.get("/fragment", response_class=HTMLResponse)
async def dashboard_fragment(request: Request, days: int = OURA_DAYS):
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
            days,
        )
        ctx.update({
            "request": request,
            "setup_mode": False,
            "error": None,
            "fragment": True,
            "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
            "vape_stats": get_vape_stats(),
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

        return JSONResponse({
            "bpm": int(latest_hr) if latest_hr is not None else None,
            "timestamp": latest_hr_timestamp
        })
    except Exception:
        return JSONResponse({"bpm": None, "timestamp": None})


@app.post("/log-vape")
async def log_vape(request: Request):
    """Log a fogger vape hit. Called via HTMX from the dashboard."""
    try:
        log_vape_session()
        return HTMLResponse(
            '<span class="text-emerald-400">Logged ✓</span>',
            headers={"HX-Trigger": "vapeLogged"}  # optional event for future use
        )
    except Exception as e:
        return HTMLResponse(
            f'<span class="text-red-400">Error logging</span>',
            status_code=500
        )


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
