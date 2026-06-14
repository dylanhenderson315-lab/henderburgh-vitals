"""Oura vitals fetching and lightweight metric helpers."""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException

from clients.oura import OuraClient, _safe_get, get_date_range, process_dashboard_data
from config import OURA_DAYS, OURA_TOKEN, PUBLIC_MODE, VITALS_SNAPSHOT_TTL
from services import state


async def fetch_all_data(days: int = OURA_DAYS) -> Dict[str, Any]:
    if not state.oura_client:
        raise HTTPException(400, "OURA_TOKEN not configured")

    start, end = get_date_range(days)
    client: OuraClient = state.oura_client

    personal, readiness, daily_sleep, detailed_sleep, activity, spo2, stress, heartrate, workouts = await asyncio.gather(
        client.get_personal_info(),
        client.get_daily_readiness(start, end),
        client.get_daily_sleep(start, end),
        client.get_sleep(start, end),
        client.get_daily_activity(start, end),
        client.get_daily_spo2(start, end),
        client.get_daily_stress(start, end),
        client.get_heartrate(start, end, bypass_cache=False),
        client.get_workouts(start, end),
        return_exceptions=True,
    )

    def safe(val, default=None):
        return val if not isinstance(val, Exception) else default

    raw = {
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

    processed = process_dashboard_data(
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
    state.last_vitals_snapshot = {"data": processed, "ts": time.time()}
    return raw


async def get_processed_vitals(days: int = OURA_DAYS, use_cache: bool = False) -> Dict[str, Any]:
    if use_cache and state.last_vitals_snapshot.get("data"):
        age = time.time() - state.last_vitals_snapshot.get("ts", 0)
        if age < VITALS_SNAPSHOT_TTL:
            return state.last_vitals_snapshot["data"]

    raw = await fetch_all_data(days)
    return process_dashboard_data(
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


def snapshot_home_metrics() -> Dict[str, Any]:
    snap = state.last_vitals_snapshot.get("data") or {}
    return {
        "steps": snap.get("steps"),
        "latest_hr": snap.get("latest_hr"),
        "hr_age_minutes": snap.get("hr_age_minutes"),
        "latest_hr_timestamp": snap.get("latest_hr_timestamp"),
    }


async def get_current_steps() -> Dict[str, Optional[Any]]:
    if not state.oura_client:
        return {"steps": None, "miles": None}
    try:
        today = date.today().isoformat()
        activity = await state.oura_client.get_daily_activity(today, today)
        if activity:
            latest = activity[0]
            steps = latest.get("steps") or 0
            miles = round(steps * 0.0005, 1) if steps else 0
            return {"steps": steps, "miles": miles}
    except Exception:
        pass
    return {"steps": None, "miles": None}


async def get_current_heart_rate() -> Dict[str, Optional[Any]]:
    if not state.oura_client:
        return {"bpm": None, "updated": None}
    try:
        now = datetime.now(timezone.utc)
        start_dt = (now - timedelta(hours=4)).isoformat()
        hr_data = await state.oura_client._get(
            "/usercollection/heartrate",
            {"start_datetime": start_dt, "end_datetime": now.isoformat()},
            bypass_cache=False,
        )
        hr_list = hr_data.get("data", []) if isinstance(hr_data, dict) else []
        if hr_list:
            latest = hr_list[-1]
            return {"bpm": latest.get("bpm"), "updated": latest.get("timestamp")}
    except Exception:
        pass
    return {"bpm": None, "updated": None}
