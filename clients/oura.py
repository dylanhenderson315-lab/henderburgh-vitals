"""Oura API client and dashboard data processing."""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException

from config import CACHE_TTL_SECONDS, DISPLAY_NAME, HEARTRATE_CACHE_TTL, OURA_BASE_URL

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
        now = time.monotonic()

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


def time_greeting_now() -> str:
    """Time-of-day fallback status (Eastern). The home page upgrades this live
    client-side with real data (Xbox / lights / heart rate)."""
    local_now = datetime.now(ZoneInfo("America/New_York"))
    total_minutes = local_now.hour * 60 + local_now.minute
    if 7 * 60 <= total_minutes < 8 * 60 + 30:
        return "HENDERBURGH IS WAKING UP."
    if 8 * 60 + 30 <= total_minutes < 17 * 60:
        return "HENDERBURGH IS WORKING."
    if 17 * 60 <= total_minutes < 22 * 60:
        return "HENDERBURGH IS RELAXING."
    if 22 * 60 <= total_minutes < 24 * 60:
        return "HENDERBURGH IS GETTING READY FOR BED."
    return "HENDERBURGH IS SLEEPING."


def _avg(vals):
    nums = [v for v in (vals or []) if v is not None]
    return sum(nums) / len(nums) if nums else None


def _band(value, bands, fallback="—"):
    """bands: list of (max_inclusive, text); first match wins. None value -> fallback."""
    if value is None:
        return fallback
    for ceiling, text in bands:
        if ceiling is None or value <= ceiling:
            return text
    return fallback


def compute_insights(ctx: Dict[str, Any]) -> Dict[str, str]:
    """Cold, quirky STATISTICS per metric — the 'steps comparison' energy (laps, bridges)
    applied everywhere: beats/day, population ranges, air volume, vs-your-own-baseline.
    Numbers and equivalences, not pep talk. Refreshes each load."""
    g = ctx.get
    ins: Dict[str, str] = {}

    def vs_avg(val, avg, lo_label):
        if val is None:
            return None
        if avg is None:
            return lo_label
        d = round(val - avg)
        if d == 0:
            return f"Dead on your 2-week average of {round(avg)}."
        return f"{'+' if d > 0 else '−'}{abs(d)} vs your 2-week average of {round(avg)}."

    ins["readiness"] = vs_avg(g("readiness_score"), g("readiness_avg"),
                              "Oura bands: 85+ optimal · 70–84 good · under 70 ease off.")
    ins["sleep"] = vs_avg(g("sleep_score"), g("sleep_avg"),
                          "Oura bands: 85+ optimal · 70–84 good · under 70 short.")
    ins["activity"] = vs_avg(g("activity_score"), g("activity_avg"),
                             "Oura bands: 85+ optimal · 70–84 good.")

    rhr = g("rhr")
    if rhr:
        ins["rhr"] = f"≈ {int(rhr * 1440):,} beats a day. Adult resting range 60–100; endurance athletes 40–60."

    hrv = g("hrv")
    if hrv:
        where = "Above" if hrv > 55 else ("Around" if hrv >= 40 else "Below")
        ins["hrv"] = f"{where} the typical adult median (~40–50 ms). HRV roughly halves every 20 years of age."

    lhr = g("latest_hr")
    if lhr:
        ins["latest_hr"] = f"≈ {int(lhr * 1440):,} beats/day at this rate. Blue-whale heart: ~8 bpm; mouse: ~600."

    rr = g("respiratory_rate")
    if rr:
        breaths = int(rr * 1440)
        ins["respiratory_rate"] = f"≈ {breaths:,} breaths/day, ~{round(breaths * 0.5 / 1000)}k litres of air. Normal adult 12–18."

    spo2 = g("spo2")
    if spo2:
        ins["spo2"] = "Normal 95–100%; under 90% is clinically low. Everest's summit air drops climbers to ~40–60%."

    td = g("temp_deviation")
    if td is not None:
        ins["temp_deviation"] = f"{td:+.1f} °C from your baseline. Oura flags a sustained +0.5 °C as a possible early illness signal."

    ts = g("total_sleep_hours")
    if ts:
        ins["sleep_dur"] = f"{ts:.1f}h — that's {round(ts / 24 * 100)}% of the day unconscious, ~{round(ts * 365 / 24)} days/year at this pace."

    ac = g("active_calories")
    if ac:
        ins["active_calories"] = f"≈ {round(ac / 10)} min of jogging, or {round(ac / 285, 1)} pizza slices earned back."

    # --- Operating State: ONE prioritized headline, worst signal wins. Factual, no fluff. ---
    r, s, h = g("readiness_score"), g("sleep_score"), g("hrv")
    if td is not None and td >= 0.5:
        ins["os_state"] = {"tone": "amber", "title": "ELEVATED TEMPERATURE",
                           "text": f"Skin temp {td:+.1f} °C over baseline — Oura's early illness signal. Worth an easy day."}
    elif r is not None and r < 70:
        ins["os_state"] = {"tone": "amber", "title": "RECOVERY FLAGGED",
                           "text": f"Readiness {r} — below the good band (70+). The data says recover, not push."}
    elif s is not None and s < 70:
        ins["os_state"] = {"tone": "amber", "title": "SHORT ON SLEEP",
                           "text": f"Sleep {s} — under the good band. Expect the mid-afternoon dip; tonight matters."}
    elif r is not None and s is not None and r >= 85 and s >= 85:
        ins["os_state"] = {"tone": "emerald", "title": "ALL SYSTEMS GREEN",
                           "text": f"Readiness {r} · sleep {s}{f' · HRV {round(h)} ms' if h else ''} — peak window. Spend it on something hard."}
    elif r is not None or s is not None:
        bits = [b for b in [f"readiness {r}" if r is not None else None,
                            f"sleep {s}" if s is not None else None,
                            f"HRV {round(h)} ms" if h else None] if b]
        ins["os_state"] = {"tone": "emerald", "title": "OPERATING NORMALLY",
                           "text": " · ".join(bits).capitalize() + " — steady state, nothing flagged."}
    return ins


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
    time_greeting = time_greeting_now()

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

    _ctx_for_insights = {
        "readiness_score": _safe_get(latest_readiness, "score"),
        "sleep_score": _safe_get(latest_daily_sleep, "score"),
        "activity_score": _safe_get(latest_activity, "score"),
        # baselines for "vs your 2-week average" comparisons
        "readiness_avg": _avg(readiness_series),
        "sleep_avg": _avg(sleep_score_series),
        "activity_avg": _avg([a.get("score") for a in activity]),
        "total_sleep_hours": (total_sleep / 3600) if total_sleep else None,
        "hrv": hrv, "rhr": rhr, "respiratory_rate": resp_rate, "spo2": spo2_val,
        "temp_deviation": temp_dev, "latest_hr": latest_hr, "active_calories": active_cal,
    }
    insights = compute_insights(_ctx_for_insights)

    return {
        "name": name,
        "insights": insights,
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


