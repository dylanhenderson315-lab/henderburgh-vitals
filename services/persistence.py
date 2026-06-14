"""JSON-backed persistence for app data."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from storage.json_store import read_json, write_json
from services import state

TV_SYNC_FILE = Path("data/tv_sync.json")

DEFAULT_TV_SYNC = {
    "rooms": {
        "game-room": {
            "lights": {},
            "mode": "true-colors",
            "settings": {"intensity": 70, "speed": 40, "brightnessLimit": 85}
        },
        "living-room": {
            "lights": {},
            "mode": "true-colors",
            "settings": {"intensity": 70, "speed": 40, "brightnessLimit": 85}
        }
    },
    "selected_room": "game-room"
}

def load_tv_sync():
    if TV_SYNC_FILE.exists():
        try:
            data = read_json(TV_SYNC_FILE, copy.deepcopy(DEFAULT_TV_SYNC))
            # merge defaults for safety (new room-based structure)
            if "rooms" not in data:
                data["rooms"] = DEFAULT_TV_SYNC["rooms"]
            if "selected_room" not in data:
                data["selected_room"] = DEFAULT_TV_SYNC["selected_room"]
            for rid, rdef in DEFAULT_TV_SYNC["rooms"].items():
                if rid not in data["rooms"]:
                    data["rooms"][rid] = rdef
                else:
                    if "lights" not in data["rooms"][rid]:
                        data["rooms"][rid]["lights"] = {}
                    if "mode" not in data["rooms"][rid]:
                        data["rooms"][rid]["mode"] = rdef["mode"]
                    if "settings" not in data["rooms"][rid]:
                        data["rooms"][rid]["settings"] = rdef["settings"]
            return data
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_TV_SYNC))

def save_tv_sync(data):
    if not isinstance(data, dict):
        data = {}
    # ensure new room-based structure
    clean = {
        "rooms": data.get("rooms") or DEFAULT_TV_SYNC["rooms"],
        "selected_room": data.get("selected_room") or DEFAULT_TV_SYNC["selected_room"]
    }
    for rid, rdef in DEFAULT_TV_SYNC["rooms"].items():
        if rid not in clean["rooms"]:
            clean["rooms"][rid] = rdef
        else:
            if "lights" not in clean["rooms"][rid]:
                clean["rooms"][rid]["lights"] = {}
            if "mode" not in clean["rooms"][rid]:
                clean["rooms"][rid]["mode"] = rdef.get("mode", "true-colors")
            if "settings" not in clean["rooms"][rid]:
                clean["rooms"][rid]["settings"] = rdef.get("settings", {})
    write_json(TV_SYNC_FILE, clean)






# =============================================================================
# Model (dedicated /model page - full spatial home replica foundation)
# Separate data file and APIs from existing TV sync / lighting to keep pages isolated.
# Clean extensible structure for rooms, lights (pos x/y 0-1 + height), furniture, modes/settings.
# =============================================================================
MODEL_FILE = Path("data/model.json")

DEFAULT_MODEL = {
    "rooms": {
        "game-room": {
            "name": "Game Room",
            "dims": {"width_ft": 16.1667, "depth_ft": 27.6667},
            "objects": [
                {"id": "seed_tv", "type": "light", "subtype": "tv-backlight", "name": "T.V Lights", "x": 0.50, "z": 0.08, "height": "tv", "scale": 1, "rotation": 0, "entity_id": None},
                {"id": "seed_couch_gr", "type": "furniture", "subtype": "couch", "name": "Sectional", "x": 0.32, "z": 0.58, "height": "floor", "scale": 1, "rotation": 0}
            ],
            "mode": "true-colors",
            "settings": {"intensity": 70, "speed": 40, "brightnessLimit": 85},
            "world": {"x": 0, "z": 0, "rot": 0},
            "locked": False
        },
        "living-room": {
            "name": "Living Room",
            "dims": {"width_ft": 13.0833, "depth_ft": 23.4167},
            "objects": [
                {"id": "seed_lamp_lr", "type": "light", "subtype": "lamp", "name": "Floor Lamp", "x": 0.22, "z": 0.35, "height": "floor", "scale": 1, "rotation": 0, "entity_id": None},
                {"id": "seed_tv_lr", "type": "tv", "subtype": "tv", "name": "TV", "x": 0.50, "z": 0.06, "height": "tv", "scale": 1, "rotation": 0}
            ],
            "mode": "true-colors",
            "settings": {"intensity": 70, "speed": 40, "brightnessLimit": 85},
            "world": {"x": -20, "z": 0, "rot": 0},
            "locked": False
        },
        "master-bedroom": {
            "name": "Master Bedroom",
            "dims": {"width_ft": 14.9167, "depth_ft": 12.5},
            "objects": [
                {"id": "seed_bed", "type": "furniture", "subtype": "bed", "name": "Bed", "x": 0.5, "z": 0.65, "height": "floor", "scale": 1, "rotation": 0}
            ],
            "mode": "true-colors",
            "settings": {"intensity": 70, "speed": 40, "brightnessLimit": 85},
            "world": {"x": 0, "z": -18, "rot": 0},
            "locked": False
        },
        "hallway": {
            "name": "Hallway",
            "dims": {"width_ft": 3.1667, "depth_ft": 18.0},
            "objects": [],
            "mode": "true-colors",
            "settings": {"intensity": 70, "speed": 40, "brightnessLimit": 85},
            "world": {"x": 17, "z": 5, "rot": 90},
            "locked": False
        },
        "kitchen": {
            "name": "Kitchen",
            "dims": {"width_ft": 12.4167, "depth_ft": 10.0},
            "objects": [
                {"id": "seed_cab1", "type": "furniture", "subtype": "table", "name": "Counter", "x": 0.15, "z": 0.2, "height": "floor", "scale": 1, "rotation": 0},
                {"id": "seed_light_k", "type": "light", "subtype": "recessed", "name": "Recessed 1", "x": 0.5, "z": 0.3, "height": "ceiling", "scale": 1, "rotation": 0, "entity_id": None}
            ],
            "mode": "true-colors",
            "settings": {"intensity": 70, "speed": 40, "brightnessLimit": 85},
            "world": {"x": -18, "z": -12, "rot": 0},
            "locked": False
        }
    },
    "selected_room": "game-room"
}

def load_model():
    if MODEL_FILE.exists():
        try:
            data = read_json(MODEL_FILE, copy.deepcopy(DEFAULT_MODEL))
            # ensure structure and defaults for scalability
            if "rooms" not in data:
                data["rooms"] = DEFAULT_MODEL["rooms"]
            if "selected_room" not in data:
                data["selected_room"] = DEFAULT_MODEL["selected_room"]
            for rid, rdef in DEFAULT_MODEL["rooms"].items():
                if rid not in data.get("rooms", {}):
                    data.setdefault("rooms", {})[rid] = rdef
                else:
                    r = data["rooms"][rid]
                    if "objects" not in r: r["objects"] = []
                    if "lights" not in r: r["lights"] = {}
                    if "mode" not in r: r["mode"] = rdef.get("mode", "true-colors")
                    if "settings" not in r: r["settings"] = rdef.get("settings", {})
                    if "furniture" not in r: r["furniture"] = []
                    if "dims" not in r: r["dims"] = rdef["dims"]
                    if "name" not in r: r["name"] = rdef["name"]
                    # Whole house support: preserve room world layout + lock
                    if "world" not in r: r["world"] = {"x": 0, "z": 0, "rot": 0}
                    if "locked" not in r: r["locked"] = False
            return data
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_MODEL))

def save_model(data):
    if not isinstance(data, dict):
        data = {}
    clean = {
        "rooms": data.get("rooms") or DEFAULT_MODEL["rooms"],
        "selected_room": data.get("selected_room") or DEFAULT_MODEL["selected_room"]
    }
    for rid, rdef in DEFAULT_MODEL["rooms"].items():
        if rid not in clean["rooms"]:
            clean["rooms"][rid] = rdef
        else:
            r = clean["rooms"][rid]
            if "objects" not in r: r["objects"] = []
            if "lights" not in r: r["lights"] = {}
            if "mode" not in r: r["mode"] = rdef.get("mode", "true-colors")
            if "settings" not in r: r["settings"] = rdef.get("settings", {})
            if "furniture" not in r: r["furniture"] = []
            if "dims" not in r: r["dims"] = rdef.get("dims", {})
            if "name" not in r: r["name"] = rdef.get("name", rid)
            # Preserve whole-house layout (world pos + lock) and future object fields (params for L-furniture, entity_id for HA link, scale/rot)
            if "world" not in r or not isinstance(r.get("world"), dict):
                r["world"] = {"x": 0, "z": 0, "rot": 0}
            if "locked" not in r:
                r["locked"] = False
            # Ensure objects carry extensible fields
            for o in r.get("objects", []):
                if "scale" not in o: o["scale"] = 1
                if "rotation" not in o: o["rotation"] = 0
                if "params" not in o and o.get("type") == "furniture" and o.get("subtype") in ("l_couch", "l_desk", "bookshelf"):
                    o["params"] = {}
                if "entity_id" not in o and o.get("type") == "light":
                    o["entity_id"] = None
    write_json(MODEL_FILE, clean)








# =============================================================================
# Xbox Game Log (persistent recently played)
# =============================================================================
XBOX_LOG_FILE = Path("data/xbox_log.json")

def load_xbox_log():
    if XBOX_LOG_FILE.exists():
        try:
            return read_json(XBOX_LOG_FILE, [])
        except Exception:
            pass
    return []

def save_xbox_log(log):
    write_json(XBOX_LOG_FILE, log)


# =============================================================================
# Home Assistant Lighting Config (custom rooms + groups, persistent)
# =============================================================================
LIGHTING_CONFIG_FILE = Path("data/lighting_config.json")

DEFAULT_LIGHTING_CONFIG = {
    "rooms": [],   # list of {"id": str, "name": str, "light_ids": list[str]}
    "groups": []   # same structure, used as Sync Groups / Pairs
}

def load_lighting_config():
    if LIGHTING_CONFIG_FILE.exists():
        try:
            config = read_json(LIGHTING_CONFIG_FILE, DEFAULT_LIGHTING_CONFIG.copy())
            if "rooms" not in config:
                config["rooms"] = []
            if "groups" not in config:
                config["groups"] = []
            return config
        except Exception:
            pass
    return DEFAULT_LIGHTING_CONFIG.copy()

def save_lighting_config(config):
    write_json(LIGHTING_CONFIG_FILE, config)

def get_persisted_lighting_structure() -> Dict[str, Any]:
    """Fast, no-HA call. Returns rooms + groups exactly as persisted + any last known light states.
    Used by /api/ha/bootstrap for instant UI render of structure before live HA data arrives.
    """
    config = load_lighting_config()
    snap = state.last_ha_lights_snapshot.get("data") or {}
    return {
        "rooms": config.get("rooms", []),
        "groups": config.get("groups", []),
        "last_states": snap.get("lights", {}),
        "total": snap.get("total", 0),
        "ts": state.last_ha_lights_snapshot.get("ts", 0),
    }


# =============================================================================
# Access Requests (for locked lighting page - simple request logging)
# =============================================================================
ACCESS_REQUESTS_FILE = Path("data/access_requests.json")

def load_access_requests():
    if ACCESS_REQUESTS_FILE.exists():
        try:
            return read_json(ACCESS_REQUESTS_FILE, [])
        except Exception:
            pass
    return []

def save_access_requests(reqs):
    write_json(ACCESS_REQUESTS_FILE, reqs)



# =============================================================================
# Golf Club Distances (server-persisted, shared across visitors)
# =============================================================================
CLUBS_FILE = Path("data/clubs.json")

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
            return read_json(CLUBS_FILE, DEFAULT_CLUBS.copy())
        except Exception:
            pass
    return DEFAULT_CLUBS.copy()

def save_clubs(clubs):
    write_json(CLUBS_FILE, clubs)


# =============================================================================
# Blog Message Board (threaded, server-persisted)
# =============================================================================

MESSAGES_FILE = Path("data/messages.json")

LAST_BLOG_READ_FILE = Path("data/last_blog_read.json")

def load_messages():
    if MESSAGES_FILE.exists():
        try:
            return read_json(MESSAGES_FILE, [])
        except Exception:
            pass
    return []

def save_messages(messages):
    write_json(MESSAGES_FILE, messages)

def load_last_blog_read():
    if LAST_BLOG_READ_FILE.exists():
        try:
            data = read_json(LAST_BLOG_READ_FILE, {"timestamp": "1970-01-01T00:00:00"})
            return data.get("timestamp", "1970-01-01T00:00:00")
        except Exception:
            pass
    return "1970-01-01T00:00:00"

def save_last_blog_read(timestamp=None):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    write_json(LAST_BLOG_READ_FILE, {"timestamp": timestamp})

