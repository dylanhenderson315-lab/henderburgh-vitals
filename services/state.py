"""Shared in-process state (single-worker deployment)."""

from __future__ import annotations

from typing import Any, Dict

last_vitals_snapshot: Dict[str, Any] = {"data": None, "ts": 0}
last_ha_lights_snapshot: Dict[str, Any] = {"data": None, "ts": 0}
last_xbox_data: Dict[str, Any] = {"status": "unavailable", "state": "Unknown", "game": "—", "last_updated": 0}
_last_xbox_fetch_time: float = 0.0
_poke_last: float = 0.0
_notify_last: float = 0.0

oura_client = None  # set at startup
