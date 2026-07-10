"""Unit tests for core dashboard logic."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Ensure test env before config import side effects
os.environ.setdefault("OURA_TOKEN", "")
os.environ.setdefault("PUBLIC_MODE", "false")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-secret")
os.environ.setdefault("XBL_API_KEY", "")
os.environ.setdefault("HA_TOKEN", "")

from clients.oura import compute_trend, process_dashboard_data
from auth import create_admin_session, is_admin_authenticated
from storage.json_store import read_json, write_json


def test_compute_trend_up():
    result = compute_trend(110, 100)
    assert result["arrow"] == "↑"
    assert result["color"] == "emerald"


def test_compute_trend_inverted_lower_is_good():
    result = compute_trend(55, 60, invert=True)
    assert result["arrow"] == "↑"
    assert result["color"] == "emerald"


def test_compute_trend_missing_values():
    result = compute_trend(None, 100)
    assert result["arrow"] == ""


def test_process_dashboard_data_minimal():
    ctx = process_dashboard_data(
        personal={"name": "Test"},
        readiness=[{"day": "2026-06-14", "score": 85}],
        daily_sleep=[{"day": "2026-06-14", "score": 80}],
        detailed_sleep=[{"day": "2026-06-14", "average_hrv": 42, "total_sleep_duration": 28800}],
        activity=[{"day": "2026-06-14", "score": 70, "steps": 8000}],
        spo2=[],
        stress=[],
        heartrate=[],
        workouts=[],
        days=7,
    )
    assert ctx["readiness_score"] == 85
    assert ctx["steps"] == 8000


def test_json_store_round_trip(tmp_path: Path):
    path = tmp_path / "data.json"
    write_json(path, {"hello": "world"})
    assert read_json(path, {}) == {"hello": "world"}


def test_admin_session_auth():
    from starlette.requests import Request

    session_id = create_admin_session()
    scope = {
        "type": "http",
        "headers": [(b"cookie", f"admin_session={session_id}".encode())],
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(scope, receive)
    assert is_admin_authenticated(request) is True


def test_ha_poke_requires_auth():
    import main

    client = TestClient(main.app)
    res = client.post("/api/ha/poke")
    assert res.status_code == 403


def test_golf_write_requires_admin():
    import main

    client = TestClient(main.app)
    res = client.post("/api/golf/clubs", json=[{"club": "Driver", "yards": 250}])
    assert res.status_code == 403


def test_guest_mode_light_control_gate(tmp_path, monkeypatch):
    """Guest Mode: open house grants light control without admin session;
    closing (or default off) rejects anonymous control. Guest toggle itself is admin-only.
    """
    import main
    from services import persistence

    # Point guest access file at a temp path so tests don't touch real data.
    guest_file = tmp_path / "guest_access.json"
    monkeypatch.setattr(persistence, "GUEST_ACCESS_FILE", guest_file)

    client = TestClient(main.app)

    # Default: locked — anonymous light control rejected
    assert persistence.guest_access_status()["enabled"] is False
    res = client.post("/api/ha/service/light/turn_on", json={"entity_id": "light.office_lamp"})
    assert res.status_code == 403

    # Anonymous cannot self-open guest access
    res = client.post("/api/ha/guest-access", json={"enabled": True, "hours": 1})
    assert res.status_code == 403

    # Admin opens guest access
    unlock = client.post("/api/auth/unlock", json={"token": "test-admin-secret"})
    assert unlock.status_code == 200
    res = client.post("/api/ha/guest-access", json={"enabled": True, "hours": 12})
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True
    assert body["expires_at"]

    # Public status reflects open house
    st = client.get("/api/auth/status").json()
    assert st["guest"] is True

    # Fresh client (no admin cookie) can pass the light-control gate while guest is open.
    # In test env HA is often not configured, so we may still get 403 from the HA_ENABLED
    # check — but it must NOT be the lock message ("Controls are locked").
    anon = TestClient(main.app)
    res = anon.post("/api/ha/service/light/turn_on", json={"entity_id": "light.office_lamp"})
    detail = (res.json() or {}).get("detail", "")
    assert "Controls are locked" not in str(detail), res.text

    # Admin closes guest access
    res = client.post("/api/ha/guest-access", json={"enabled": False})
    assert res.status_code == 200
    assert res.json()["enabled"] is False

    # Anonymous locked again at the light-control gate
    res = anon.post("/api/ha/service/light/turn_on", json={"entity_id": "light.office_lamp"})
    assert res.status_code == 403
    assert "Controls are locked" in str((res.json() or {}).get("detail", ""))


def test_synth_lights_keeps_structure_when_ha_down():
    from services.home_assistant import _synth_lights_from_config_and_snapshot

    rooms = [{"id": "hallway", "name": "Hallway", "light_ids": ["light.hallway_light_1", "light.hallway_light_2"]}]
    groups = []
    out = _synth_lights_from_config_and_snapshot(rooms, groups)
    assert out["ha_ok"] is False
    assert out["total_lights"] == 2
    assert len(out["lights_by_room"]["Hallway"]) == 2
    assert out["rooms"][0]["light_count"] == 2
