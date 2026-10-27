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
os.environ.setdefault("HA_TOKEN", "test-ha-token")

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


def test_ha_poke_is_public_but_rate_limited(monkeypatch):
    # Poke is public on purpose (friends can blink the office lamp); the guard is
    # the 3s rate limit. Stub the blink so the test never touches a real lamp.
    import main

    async def no_blink():
        return None

    monkeypatch.setattr(main.home_assistant, "perform_poke_blink", no_blink)
    monkeypatch.setattr(main.state, "_poke_last", 0.0)
    client = TestClient(main.app)
    assert client.post("/api/ha/poke").status_code == 200
    assert client.post("/api/ha/poke").status_code == 429


def test_hub_is_installable_and_ipad_redirects():
    import main

    client = TestClient(main.app)
    res = client.get("/hub")
    assert res.status_code == 200
    assert 'rel="manifest"' in res.text
    assert "apple-touch-icon" in res.text
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["start_url"] == "/hub"
    assert client.get("/sw.js").headers["content-type"].startswith("application/javascript")
    old = client.get("/ipad", follow_redirects=False)
    assert old.status_code == 307 and old.headers["location"] == "/hub"


def test_house_status_cascade_matches_front_door():
    import main

    lights_on = {"lights_on": 3, "total_lights": 14, "rooms": [{"name": "Kitchen", "on": 2}, {"name": "Hallway", "on": 1}]}
    dark = {"lights_on": 0, "total_lights": 14, "rooms": []}
    playing = {"status": "ok", "state": "Online", "game": "NASCAR 26", "kind": "game"}
    music = {"now_playing": {"track": "My Love", "artist": "Justin Timberlake"}}

    assert main._house_status(playing, music, lights_on, 72, 20.0)["state"] == "playing NASCAR 26"
    assert main._house_status({"status": "ok", "state": "Online", "game": "Home"}, music, lights_on, None, 20.0)["state"] == "listening to My Love"
    night = main._house_status(None, {}, dark, None, 23.5)
    assert night["state"] == "sleeping" and night["live"] is False
    assert main._house_status(None, {}, dark, None, 14.0)["state"] == "out"
    day = main._house_status(None, {}, lights_on, 70, 10.0)
    assert day["state"] == "working"
    assert day["sub"] == "3 of 14 lights on · Kitchen lit · heart 70 bpm"


def test_api_home_reports_weather_and_sensors(monkeypatch):
    import main

    async def fake_states():
        return [
            {"entity_id": "weather.forecast_home", "state": "sunny",
             "attributes": {"temperature": 71, "temperature_unit": "°F", "humidity": 55}},
            {"entity_id": "sensor.office_temp", "state": "70.2",
             "attributes": {"device_class": "temperature", "unit_of_measurement": "°F", "friendly_name": "Office"}},
            {"entity_id": "sensor.dead_temp", "state": "unavailable",
             "attributes": {"device_class": "temperature"}},
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
        ], True

    monkeypatch.setattr(main.home_assistant, "fetch_ha_states", fake_states)
    monkeypatch.setenv("ARCADE_URL", "http://127.0.0.1:9")   # nothing listens: arcade must degrade to null
    body = TestClient(main.app).get("/api/home").json()
    assert body["ha_ok"] is True
    assert body["weather"]["temperature"] == 71
    assert [s["name"] for s in body["sensors"]] == ["Office"]
    assert body["arcade"] is None


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


def test_media_tvs_lists_remotes_with_now_playing(monkeypatch):
    import main

    async def fake_states():
        return [
            {"entity_id": "remote.living_room_tv", "state": "on",
             "attributes": {"friendly_name": "Living Room TV", "current_activity": "com.netflix.ninja"}},
            {"entity_id": "media_player.living_room_tv", "state": "playing",
             "attributes": {"media_title": "Arcane"}},
            {"entity_id": "remote.bedroom_tv", "state": "off", "attributes": {"friendly_name": "Bedroom TV"}},
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
        ], True

    monkeypatch.setattr(main.home_assistant, "fetch_ha_states", fake_states)
    tvs = TestClient(main.app).get("/api/media/tvs").json()["tvs"]
    assert [t["name"] for t in tvs] == ["Bedroom TV", "Living Room TV"]
    living = tvs[1]
    assert living["on"] and living["app"] == "com.netflix.ninja" and living["title"] == "Arcane"


def test_media_launch_is_gated_and_validated(monkeypatch):
    import main

    calls = []

    async def fake_call(domain, service, entity_id=None, extra=None):
        calls.append((domain, service, entity_id, extra))
        return {}

    monkeypatch.setattr(main.home_assistant, "call_ha_service", fake_call)
    client = TestClient(main.app)
    body = {"tv": "remote.living_room_tv", "app": "com.netflix.ninja"}
    assert client.post("/api/media/launch", json=body).status_code == 403   # locked without a session

    monkeypatch.setattr(main, "require_light_control", lambda request: None)
    assert client.post("/api/media/launch", json={"tv": "light.kitchen", "app": "com.netflix.ninja"}).status_code == 400
    assert client.post("/api/media/launch", json={"tv": "remote.x", "app": "javascript:alert(1)"}).status_code == 400
    assert client.post("/api/media/command", json={"tv": "remote.x", "command": "REBOOT"}).status_code == 400
    assert client.post("/api/media/launch", json=body).status_code == 200
    assert calls[-1] == ("remote", "turn_on", "remote.living_room_tv", {"activity": "com.netflix.ninja"})


def test_watch_maps_networks_to_owned_apps():
    from services import watch

    assert watch.apps_for(["ESPN", "ABC", "Disney+"]) == ["disney"]    # MNF: ESPN+ isn't ESPN; Disney+ simulcasts
    assert watch.apps_for(["ESPN+"]) == ["espn"]
    assert watch.apps_for(["ESPN"]) == []
    assert watch.apps_for(["Fox"]) == []                        # not in his apps -> live channels
    assert watch.apps_for(["TNT"]) == ["max"]
    assert watch.apps_for(["Prime Video"]) == ["prime"]         # Thursday Night Football
    assert watch.apps_for(["ABC"], sports=False) == []
    assert watch.apps_for(["NBC"], sports=False) == ["peacock"]


def test_watch_close_game_and_pick(monkeypatch):
    import asyncio
    from services import watch

    def team(abbr, score, home):
        return {"abbr": abbr, "name": abbr, "color": "112233", "score": score, "home": home, "record": None, "logo": None, "alt": None}

    live = {"id": "1", "league": "NFL", "start": "2026-10-27T00:15Z", "state": "in", "detail": "Q4 2:01", "period": 4,
            "clock": "2:01", "teams": [team("DAL", "20", False), team("PHI", "24", True)], "networks": ["ESPN"],
            "apps": ["espn"], "national": True, "postseason": False, "note": None, "series": None, "situation": None, "win": None}
    assert watch._closeness(live) == "Close · late"
    blowout = dict(live, teams=[team("DAL", "3", False), team("PHI", "38", True)])
    assert watch._closeness(blowout) is None

    async def fake_board(client, league, ymd):
        return [dict(live)] if league == "NFL" else []

    async def fake_guide(client, ymd):
        return [{"kind": "show", "time": "20:00", "title": "World Series", "episode": "Game 3", "network": "FOX",
                 "apps": [], "genres": [], "sports": True},
                {"kind": "show", "time": "21:00", "title": "FBI", "episode": "x", "network": "CBS",
                 "apps": ["paramount"], "genres": ["Drama"], "sports": False}]

    async def no_wp(client, g):
        return {"home": 71}

    monkeypatch.setattr(watch, "_scoreboard", fake_board)
    monkeypatch.setattr(watch, "_guide", fake_guide)
    monkeypatch.setattr(watch, "_win_prob", no_wp)
    out = asyncio.run(watch.tonight())
    assert out["pick"]["kind"] == "game" and out["pick"]["state"] == "in"
    assert out["pick"]["reason"].startswith("Close · late: DAL 20–24 PHI")
    assert out["live"][0]["win"] == {"home": 71}
    assert [i["title"] for i in out["guide"]] == ["FBI"]          # sports never doubles up in the TV guide


def test_streaming_redirects_to_hub_watch():
    import main

    res = TestClient(main.app).get("/streaming", follow_redirects=False)
    assert res.status_code == 307 and res.headers["location"] == "/hub#watch"


def _close_game():
    def team(abbr, score):
        return {"abbr": abbr, "name": abbr, "color": "112233", "score": score, "home": abbr == "PHI", "record": None, "logo": None, "alt": None}
    return {"id": "401", "league": "NFL", "start": "2026-10-27T00:15Z", "state": "in", "detail": "Q4 2:01", "period": 4,
            "clock": "2:01", "teams": [team("DAL", "20"), team("PHI", "24")], "networks": ["ESPN+"], "apps": ["espn"],
            "national": True, "postseason": False, "note": None, "series": None, "situation": "3rd & 4 at DAL 30",
            "win": None, "close": "Close · late", "score": 160}


def test_watch_put_tokens_are_scoped_and_expire():
    from services import watch

    tok = watch.sign_put("401", "remote.smart_tv", "espn", now=1_000_000)
    assert watch.verify_put(tok, now=1_000_100) == {"game_id": "401", "tv": "remote.smart_tv", "app": "espn"}
    assert watch.verify_put(tok, now=1_000_000 + watch.ALERT_TTL + 1) is None        # expired
    assert watch.verify_put(tok.replace("remote.smart_tv", "remote.other_tv"), now=1_000_100) is None   # tampered
    assert watch.verify_put("garbage") is None


def test_close_game_alert_has_tv_buttons():
    from services import watch

    tvs = [{"entity_id": "remote.smarttv_4k_ffm", "name": "Living Room TV", "on": False, "available": True},
           {"entity_id": "remote.smart_tv", "name": "Theater TV", "on": True, "available": True}]
    al = watch.build_alert(_close_game(), tvs, "https://henderburgh.com")
    assert al["title"] == "Close & late: DAL 20-24 PHI"
    assert al["body"] == "Q4 2:01 · 3rd & 4 at DAL 30 · in ESPN+"
    labels = [a["label"] for a in al["actions"]]
    assert labels == ["ESPN+ on Theater TV", "ESPN+ on Living Room TV", "Open Watch"]   # the TV that's on comes first
    assert al["actions"][0]["method"] == "POST" and "/api/watch/put?t=" in al["actions"][0]["url"]


def test_close_game_alert_fires_once(monkeypatch, tmp_path):
    import asyncio
    from datetime import datetime
    from services import watch

    real_hours = watch.alert_hours
    monkeypatch.setattr(watch, "SENT_PATH", tmp_path / "sent.json")
    monkeypatch.setattr(watch, "alert_hours", lambda now: True)

    async def fake_tonight():
        return {"live": [_close_game()]}

    monkeypatch.setattr(watch, "tonight", fake_tonight)
    pushes = []
    notify = lambda *a: pushes.append(a) or True
    assert asyncio.run(watch.check_close_games([], notify, "https://x")) == ["401"]
    assert asyncio.run(watch.check_close_games([], notify, "https://x")) == []          # deduped
    assert len(pushes) == 1
    assert real_hours(datetime(2026, 10, 27, 3, 0)) is False                            # 3 AM: quiet
    assert real_hours(datetime(2026, 10, 27, 0, 45)) is True                            # West Coast finish


def test_watch_put_endpoint(monkeypatch):
    import main
    from services import watch

    calls = []

    async def fake_call(domain, service, entity_id=None, extra=None):
        calls.append((domain, service, entity_id, extra))
        return {}

    monkeypatch.setattr(main.home_assistant, "call_ha_service", fake_call)
    monkeypatch.setattr(main, "HA_ENABLED", True)
    client = TestClient(main.app)
    assert client.post("/api/watch/put?t=nope").status_code == 403
    tok = watch.sign_put("401", "remote.smart_tv", "espn")
    assert client.get(f"/api/watch/put?t={tok}").status_code == 200 and not calls        # GET never touches the TV
    assert client.post(f"/api/watch/put?t={tok}").status_code == 200
    # The Theater TV has an HTC box: apps launch on the box so the light kit sees them.
    assert calls[-1] == ("remote", "turn_on", "remote.frc1_mobi", {"activity": "https://www.espn.com/watch/"})


def test_watch_free_legal_apps_map():
    from services import watch

    assert watch.apps_for(["Tubi"], sports=False) == ["tubi"]
    assert watch.apps_for(["Pluto TV"], sports=False) == ["pluto"]
    assert set(["tubi", "pluto", "roku", "plex"]) <= set(watch.APP_LINKS)


def test_watch_find_teams_and_shows(monkeypatch):
    import asyncio
    from services import watch

    index = [{"league": "NFL", "id": "29", "name": "Carolina Panthers", "short": "Panthers", "nick": "Panthers", "loc": "Carolina", "abbr": "CAR", "color": "0085ca", "logo": None},
             {"league": "NFL", "id": "9", "name": "Green Bay Packers", "short": "Packers", "nick": "Packers", "loc": "Green Bay", "abbr": "GB", "color": "", "logo": None}]
    assert [t["abbr"] for t in watch._match_teams(index, "panthers")] == ["CAR"]
    assert [t["abbr"] for t in watch._match_teams(index, "gb")] == ["GB"]
    assert watch._match_teams(index, "xx") == []


def test_sunset_matches_myrtle_beach():
    from datetime import date
    from zoneinfo import ZoneInfo
    from services.sky import sun_times

    et = ZoneInfo("America/New_York")
    t = sun_times(33.73, -78.90, date(2026, 10, 26))
    assert t["sunset"].astimezone(et).strftime("%H:%M") in ("18:29", "18:30", "18:31")
    assert t["sunrise"].astimezone(et).strftime("%H:%M") in ("07:27", "07:28", "07:29")


def test_favorites_detects_his_teams_and_kickoffs():
    from datetime import datetime, timezone
    from services import favorites

    steelers_game = {"league": "NFL", "teams": [{"id": "5", "abbr": "CLE"}, {"id": "23", "abbr": "PIT"}]}
    pirates_id_in_nfl = {"league": "NFL", "teams": [{"id": "1", "abbr": "ATL"}, {"id": "2", "abbr": "BUF"}]}
    pitt = {"league": "NCAAF", "teams": [{"id": "59", "abbr": "GT"}, {"id": "221", "abbr": "PITT"}]}
    assert favorites.is_favorite(steelers_game) and favorites.is_favorite(pitt)
    assert not favorites.is_favorite(pirates_id_in_nfl)

    now = datetime(2026, 11, 1, 17, 50, tzinfo=timezone.utc)
    teams = [{"name": "Pittsburgh Steelers", "next": {"start": "2026-11-01T18:00Z", "live": False}},
             {"name": "Pittsburgh Penguins", "next": {"start": "2026-11-01T23:00Z", "live": False}}]
    soon = favorites.kickoff_soon(teams, now, 15)
    assert [s["team"]["name"] for s in soon] == ["Pittsburgh Steelers"] and soon[0]["minutes"] == 10


def test_gamecenter_rates_and_edges():
    from services import gamecenter as gc
    assert gc._rate("1/4") == 25.0 and gc._rate("41-85") == 48.2 and gc._rate("28") == 28.0
    p = gc._pair("Power play", "1/4", "1/3", False)
    assert p["edge"] == "home"                      # 33% beats 25%, not 1 vs 1
    assert gc._pair("Turnovers", "3", "1", True)["edge"] == "home"
    assert gc._pair("Shots", None, None, False) is None


def test_ufc_event_is_shaped_like_a_game():
    from services import combat
    ev = {"id": "1", "name": "UFC 330: A vs. B", "date": "2026-11-01T22:00Z", "status": {"type": {"state": "pre"}},
          "competitions": [{"date": "2026-11-02T01:00Z", "type": {"abbreviation": "Lightweight"},
                            "status": {"type": {"state": "pre"}}, "broadcasts": [{"names": ["Paramount+"]}],
                            "competitors": [{"id": "9", "order": 1, "athlete": {"displayName": "Ann Ace"}, "records": [{"summary": "10-1"}]},
                                            {"id": "8", "order": 2, "athlete": {"displayName": "Bea Bold"}}]}]}
    g = combat._ufc_event(ev)
    assert g["league"] == "UFC" and g["apps"] == ["paramount"] and g["big"] and g["start"] == "2026-11-02T01:00Z"
    assert [t["abbr"] for t in g["teams"]] == ["ACE", "BOLD"] and g["teams"][0]["record"] == "10-1"
    from services.watch import _closeness
    g["state"] = "in"
    assert _closeness(g) is None                     # fights are never "close · late"


def test_guide_merge_games_win_and_clip():
    from datetime import datetime, timezone
    from services import guide
    t0 = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
    show = {"start": "2026-10-27T01:00:00+00:00", "end": "2026-10-27T02:00:00+00:00", "title": "Filler", "kind": "show"}
    old = {"start": "2026-10-26T20:00:00+00:00", "end": "2026-10-26T21:00:00+00:00", "title": "Gone", "kind": "show"}
    game = {"start": "2026-10-27T00:30:00+00:00", "end": "2026-10-27T03:45:00+00:00", "title": "Pitt at GT", "kind": "game"}
    out = guide._merge([show, old], [game], t0, t1)
    assert [p["title"] for p in out] == ["Pitt at GT"]          # the game replaces the filler; past shows drop


def test_htc_lineup_is_sorted_and_maps_networks():
    from services import htc
    nums = [n for n, _, _ in htc.LINEUP]
    assert nums == sorted(nums) and len(nums) == len(set(nums))
    assert htc.channel_for("ESPN2") == 29 and htc.channel_for("Fox News Channel") == 61 and htc.channel_for("NFL Network") is None


def test_epg_parse_maps_and_cleans():
    import gzip
    from datetime import datetime, timezone
    from services import epg
    xml = (b'<?xml version="1.0"?><tv><channel id="I206.10179.schedulesdirect.org"><display-name>ESPN</display-name></channel>'
           b'<programme start="20261027001500 +0000" stop="20261027031500 +0000" channel="I206.10179.schedulesdirect.org">'
           b'<title>NFL Football [NEW]</title><sub-title>Dallas \xf0\x9f\x8f\x88 [NFL] Cowboys at Philadelphia</sub-title>'
           b'<category>Football</category><new/></programme>'
           b'<programme start="20261027001500 +0000" stop="20261027031500 +0000" channel="nope"><title>x</title></programme></tv>')
    out = epg._parse(gzip.compress(xml), datetime(2026, 10, 26, tzinfo=timezone.utc), datetime(2026, 10, 28, tzinfo=timezone.utc))
    assert list(out) == [28]
    p = out[28][0]
    assert p["title"] == "NFL Football" and p["new"] and p["kind"] == "sports" and "\U0001F3C8" not in p["sub"] and "[NFL]" not in p["sub"]
