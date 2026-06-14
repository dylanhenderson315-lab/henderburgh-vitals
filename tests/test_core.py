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
