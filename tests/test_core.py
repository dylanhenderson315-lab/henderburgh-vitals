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
from gtm import inject, normalize_container_id
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

    # Public status reflects open house (can_control even without admin cookie)
    st = client.get("/api/auth/status").json()
    assert st["guest"] is True
    assert st["can_control"] is True
    # Fresh anonymous status also can_control
    anon_status = TestClient(main.app).get("/api/auth/status").json()
    assert anon_status["unlocked"] is False
    assert anon_status["guest"] is True
    assert anon_status["can_control"] is True

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


def test_clips_upload_and_list(tmp_path, monkeypatch):
    """Phone upload saves into DATA_DIR/clips; list + delete work for admin only."""
    import io
    import main
    from services import persistence

    data_path = tmp_path / "data"
    data_path.mkdir()
    monkeypatch.setattr(persistence, "DATA_PATH", data_path)

    client = TestClient(main.app)

    # Anonymous cannot upload
    res = client.post(
        "/api/clips/upload",
        files={"file": ("test.mp4", io.BytesIO(b"fake-video-bytes"), "video/mp4")},
        data={"title": "Clutch"},
    )
    assert res.status_code == 403

    unlock = client.post("/api/auth/unlock", json={"token": "test-admin-secret"})
    assert unlock.status_code == 200

    res = client.post(
        "/api/clips/upload",
        files={"file": ("clutch-1v3.mp4", io.BytesIO(b"fake-video-bytes-123"), "video/mp4")},
        data={"title": "Clutch 1v3"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok"
    name = body["clip"]["name"]
    assert name.endswith(".mp4")
    assert (data_path / "clips" / name).is_file()

    listed = client.get("/api/clips").json()["clips"]
    assert any(c["name"] == name for c in listed)

    # Media serves
    media = client.get(f"/media/clips/{name}")
    assert media.status_code == 200

    # Delete
    res = client.delete(f"/api/clips/{name}")
    assert res.status_code == 200
    assert not (data_path / "clips" / name).exists()


def test_blog_delete_requires_admin_and_works(tmp_path, monkeypatch):
    """Blog delete was broken: handler called require_admin(request) without injecting Request."""
    import main
    from services import persistence

    msg_file = tmp_path / "messages.json"
    monkeypatch.setattr(persistence, "MESSAGES_FILE", msg_file)

    client = TestClient(main.app)

    # Anonymous cannot delete
    res = client.delete("/api/messages/nope")
    assert res.status_code == 403

    # Unlock as admin
    unlock = client.post("/api/auth/unlock", json={"token": "test-admin-secret"})
    assert unlock.status_code == 200

    # Create a top-level post + reply
    parent = client.post("/api/messages", json={"name": "Dylan", "text": "parent post"}).json()
    reply = client.post(
        "/api/messages",
        json={"name": "Friend", "text": "reply", "parent_id": parent["id"]},
    ).json()
    assert parent.get("id") and reply.get("id")

    # Delete parent — reply should go too
    res = client.delete(f"/api/messages/{parent['id']}")
    assert res.status_code == 200, res.text
    remaining = client.get("/api/messages").json()
    ids = {m.get("id") for m in remaining}
    assert parent["id"] not in ids
    assert reply["id"] not in ids


def test_gtm_id_must_be_real_container():
    assert normalize_container_id("GTM-ABC123") == "GTM-ABC123"
    assert normalize_container_id("gtm-abc123") == "GTM-ABC123"
    assert normalize_container_id("G-XXXX") == ""
    assert normalize_container_id("") == ""
    assert normalize_container_id("not-a-tag") == ""


def test_gtm_injects_into_full_page_only():
    page = "<!DOCTYPE html><html><head><title>x</title></head><body><p>hi</p></body></html>"
    out = inject(page, "GTM-TEST1")
    assert "googletagmanager.com/gtm.js" in out
    assert "GTM-TEST1" in out
    assert "ns.html?id=GTM-TEST1" in out
    # fragment / HTMX piece — do not inject
    assert "gtm.js" not in inject("<div>card</div>", "GTM-TEST1")
    # already tagged — do not double
    assert out.count("gtm.js") == inject(out, "GTM-TEST1").count("gtm.js")
