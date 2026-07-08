"""Home Assistant integration helpers."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import HTTPException

from config import HA_ENABLED, HA_HEADERS, HA_URL, OFFICE_LAMP
from services import persistence, state

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


# =============================================================================
# Orchestrated effects — dynamic scenes WiZ doesn't ship, conducted by the
# server as a cancellable loop against any set of lights. One orchestration
# runs at a time (starting a new one replaces the old). Auto-ends after 8h;
# stopping restores calm warm light instead of leaving a scene mid-frame.
# =============================================================================
_fx_task: Optional[asyncio.Task] = None
_fx_entities: List[str] = []
_fx_pattern: str = ""

FX_MAX_SECONDS = 8 * 3600


async def _fx_thunderstorm(ids: List[str], rnd):
    """Dim blue-grey storm base; randomized 1–3 rapid lightning strikes every 12–45s."""
    base = {"rgb_color": [30, 40, 70], "brightness_pct": 8, "transition": 2}
    await call_ha_service("light", "turn_on", ids, dict(base))
    while True:
        await asyncio.sleep(rnd.uniform(12, 45))
        for _ in range(rnd.randint(1, 3)):
            await call_ha_service("light", "turn_on", ids,
                                  {"rgb_color": [215, 228, 255], "brightness_pct": 100, "transition": 0})
            await asyncio.sleep(rnd.uniform(0.12, 0.30))
            await call_ha_service("light", "turn_on", ids,
                                  {"rgb_color": [60, 75, 120], "brightness_pct": 18, "transition": 0})
            await asyncio.sleep(rnd.uniform(0.15, 0.55))
        await call_ha_service("light", "turn_on", ids, dict(base))


async def _fx_aurora(ids: List[str], rnd):
    """Northern lights: slow drifting waves of green / teal / violet."""
    palette = [[20, 190, 120], [30, 160, 190], [110, 70, 200], [20, 200, 170], [60, 120, 220]]
    while True:
        c = palette[rnd.randrange(len(palette))]
        await call_ha_service("light", "turn_on", ids,
                              {"rgb_color": c, "brightness_pct": rnd.randint(25, 45), "transition": 5})
        await asyncio.sleep(rnd.uniform(6, 10))


async def _fx_snowy_sky(ids: List[str], rnd):
    """The WiZ-app scene HA never got: gentle drifting cool whites / pale blues."""
    palette = [[225, 238, 255], [195, 215, 250], [240, 246, 255], [175, 200, 245]]
    while True:
        c = palette[rnd.randrange(len(palette))]
        await call_ha_service("light", "turn_on", ids,
                              {"rgb_color": c, "brightness_pct": rnd.randint(30, 60), "transition": 4})
        await asyncio.sleep(rnd.uniform(5, 8))


async def _fx_lava_lamp(ids: List[str], rnd):
    """Slow morphing warm reds / oranges / magentas, like a lava lamp."""
    palette = [[255, 60, 10], [255, 120, 0], [200, 20, 110], [255, 40, 60], [230, 90, 20]]
    while True:
        c = palette[rnd.randrange(len(palette))]
        await call_ha_service("light", "turn_on", ids,
                              {"rgb_color": c, "brightness_pct": rnd.randint(35, 55), "transition": 6})
        await asyncio.sleep(rnd.uniform(7, 11))


FX_PATTERNS = {
    "thunderstorm": _fx_thunderstorm,
    "aurora": _fx_aurora,
    "snowy-sky": _fx_snowy_sky,
    "lava-lamp": _fx_lava_lamp,
}


async def _fx_runner(pattern: str, ids: List[str]):
    import random
    rnd = random.Random()
    try:
        await asyncio.wait_for(FX_PATTERNS[pattern](ids, rnd), timeout=FX_MAX_SECONDS)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception as e:
        print(f"orchestrated effect '{pattern}' error: {e}")


def fx_status() -> Dict[str, Any]:
    running = _fx_task is not None and not _fx_task.done()
    return {"running": running,
            "pattern": _fx_pattern if running else None,
            "entities": _fx_entities if running else []}


async def fx_start(pattern: str, entity_ids: List[str]) -> Dict[str, Any]:
    global _fx_task, _fx_entities, _fx_pattern
    if pattern not in FX_PATTERNS:
        raise HTTPException(400, f"Unknown pattern. Available: {', '.join(FX_PATTERNS)}")
    await fx_stop(restore=False)
    _fx_entities, _fx_pattern = entity_ids, pattern
    _fx_task = asyncio.create_task(_fx_runner(pattern, entity_ids))
    return fx_status()


async def fx_stop(restore: bool = True) -> Dict[str, Any]:
    global _fx_task, _fx_entities, _fx_pattern
    if _fx_task and not _fx_task.done():
        _fx_task.cancel()
        try:
            await _fx_task
        except Exception:
            pass
        if restore and _fx_entities:
            try:
                await call_ha_service("light", "turn_on", _fx_entities,
                                      {"color_temp_kelvin": 2700, "brightness_pct": 25, "transition": 2})
            except Exception:
                pass
    _fx_task = None
    _fx_entities = []
    _fx_pattern = ""
    return fx_status()


async def perform_poke_blink():
    """Backend-handled snappy poke: instant off for 0.25s then back on to previous state.
    Runs in background so endpoint returns immediately for instant feel.
    """
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
    """Backend-handled notification (used for new blog posts): instantaneous blue flash for ~0.25s
    then immediately restore to previous state (exactly like perform_poke_blink but using blue
    color instead of off). Uses transition=0 for snap on/off. Runs in background so caller
    (e.g. /api/messages) returns instantly. Does not modify poke behavior.
    """
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

        # instantaneous blue (same blue values as before)
        blue = {
            "brightness": 200,
            "rgb_color": [0, 80, 255],
            "transition": 0,
        }
        await call_ha_service("light", "turn_on", OFFICE_LAMP, blue)
        await asyncio.sleep(0.25)

        # restore to previous (identical logic and trans=0 to poke_blink)
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
        print(f"perform_notify_blue error: {e}")


async def get_ha_lights_data():
    """Rich data for lighting dashboard.
    Uses HA for discovering lights + effects.
    Uses exact rooms/groups from lighting_config.json for organization (user can create, move lights, rename, etc.).
    No auto-seeding - config.json is the source of truth for room assignments.
    New/unmatched lights go to Unassigned.
    Updates the state.last_ha_lights_snapshot so bootstrap + "last known" are instant on next loads.
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

    config = persistence.load_lighting_config()
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
    lights_map = {}
    for l in all_lights:
        lights_map[l["entity_id"]] = {
            "state": l["state"],
            "brightness": l.get("brightness"),
            "current_effect": l.get("current_effect"),
            "color_temp": l.get("color_temp"),
            "rgb_color": l.get("rgb_color"),
        }
    state.last_ha_lights_snapshot = {
        "data": {"lights": lights_map, "total": len(all_lights)},
        "ts": time.time()
    }

    return result


