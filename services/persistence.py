"""JSON-backed persistence for app data."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from storage.json_store import read_json, write_json
from services import state
from config import DATA_DIR as _DATA_DIR

# Resolve the persistence directory once. On Railway set DATA_DIR=/data (a mounted
# volume) so writes survive redeploys; locally it falls back to ./data.
DATA_PATH = Path(_DATA_DIR)
DATA_PATH.mkdir(parents=True, exist_ok=True)

# Bundled defaults shipped in the image (./data). On first run against an empty
# volume we copy these in so the app starts with the committed blueprint/layout.
_BUNDLED_DATA = Path(__file__).resolve().parent.parent / 'data'
def _seed(name: str):
    dest = DATA_PATH / name
    src = _BUNDLED_DATA / name
    if not dest.exists() and src.exists() and src.resolve() != dest.resolve():
        try:
            dest.write_text(src.read_text(encoding='utf-8'), encoding='utf-8')
        except Exception:
            pass

# Force a one-time overwrite of the persisted model from the committed baseline.
# Use ONLY to recover after data loss: bump MODEL_SEED_VERSION, redeploy once, and the
# persisted file (even on a volume) is replaced by the bundled baseline, then left alone
# again (the stored seed_version matches), so future edits persist normally.
MODEL_SEED_VERSION = 2

def _restore_model_if_needed():
    dest = DATA_PATH / "model.json"
    src = _BUNDLED_DATA / "model.json"
    if not src.exists() or src.resolve() == dest.resolve():
        return
    try:
        cur = json.loads(dest.read_text(encoding='utf-8')) if dest.exists() else {}
    except Exception:
        cur = {}
    if int(cur.get("seed_version", 0)) >= MODEL_SEED_VERSION:
        return  # already at/over the recovery version — never clobber live edits again
    try:
        data = json.loads(src.read_text(encoding='utf-8'))
        data["seed_version"] = MODEL_SEED_VERSION
        # keep a TIMESTAMPED copy of whatever was there before (never overwrite a recovery point)
        if dest.exists():
            from datetime import datetime as _dt, timezone as _tz
            _stamp = _dt.now(_tz.utc).strftime("%Y%m%d-%H%M%S")
            bdir = DATA_PATH / "model_backups"; bdir.mkdir(exist_ok=True)
            (bdir / f"prerestore-{_stamp}.json").write_text(dest.read_text(encoding='utf-8'), encoding='utf-8')
        dest.write_text(json.dumps(data, indent=2), encoding='utf-8')
        print(f"♻️  Restored model.json from baseline (seed_version -> {MODEL_SEED_VERSION})")
    except Exception as e:
        print(f"model restore skipped: {e}")

TV_SYNC_FILE = _seed("tv_sync.json") or (DATA_PATH / "tv_sync.json")

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
MODEL_FILE = _seed("model.json") or (DATA_PATH / "model.json")
_restore_model_if_needed()  # one-time recovery overwrite when MODEL_SEED_VERSION is bumped

DEFAULT_MODEL = {
    "rooms": {
        "game-room": {
            "name": "Game Room",
            "dims": {"width_ft": 9.0, "depth_ft": 16.0},
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
            "dims": {"width_ft": 12.1667, "depth_ft": 11.8333},
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
            "dims": {"width_ft": 3.1667, "depth_ft": 16.0},
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
        },
        "3rd-bedroom": {
            "name": "3rd Bedroom",
            "dims": {"width_ft": 11.3333, "depth_ft": 16.3333},
            "objects": [],
            "mode": "true-colors",
            "settings": {"intensity": 70, "speed": 40, "brightnessLimit": 85},
            "world": {"x": 5.6667, "z": 9.1667, "rot": 0},
            "locked": False
        },
        "entry-foyer": {
            "name": "Entry / Foyer",
            "dims": {"width_ft": 12.1667, "depth_ft": 6.0},
            "objects": [],
            "filler": True,
            "mode": "true-colors",
            "settings": {"intensity": 70, "speed": 40, "brightnessLimit": 85},
            "world": {"x": 19.1667, "z": 3.0, "rot": 0},
            "locked": True
        },
        "baths-closets": {
            "name": "Baths & Closets",
            "dims": {"width_ft": 12.1667, "depth_ft": 16.9167},
            "objects": [],
            "filler": True,
            "mode": "true-colors",
            "settings": {"intensity": 70, "speed": 40, "brightnessLimit": 85},
            "world": {"x": 19.1667, "z": 30.4583, "rot": 0},
            "locked": True
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
            data.setdefault("rev", 0)  # concurrency revision for the stale-write guard
            return data
        except Exception:
            pass
    d = json.loads(json.dumps(DEFAULT_MODEL))
    d.setdefault("rev", 0)
    return d

class StaleWriteError(Exception):
    """Raised when a save is based on an older revision than what's stored (stale tab)."""


def list_model_backups():
    """Recovery: all saved model snapshots (newest first) with their object counts."""
    out = []
    bdir = DATA_PATH / "model_backups"
    candidates = []
    if (DATA_PATH / "model.bak.json").exists():
        candidates.append(DATA_PATH / "model.bak.json")
    if (DATA_PATH / "model.pre-restore.json").exists():
        candidates.append(DATA_PATH / "model.pre-restore.json")
    if bdir.exists():
        candidates += sorted(bdir.glob("*.json"), reverse=True)
    for p in candidates:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            objs = sum(len(r.get("objects", [])) for r in d.get("rooms", {}).values())
            out.append({"name": p.name, "objects": objs, "rev": d.get("rev"), "seed_version": d.get("seed_version")})
        except Exception:
            continue
    return out


def read_model_backup(name: str):
    """Return a named backup's parsed content (recovery)."""
    safe = Path(name).name  # no path traversal
    for p in (DATA_PATH / safe, DATA_PATH / "model_backups" / safe):
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def save_model(data):
    if not isinstance(data, dict):
        data = {}

    # --- Stale-write guard (prevents an old tab from clobbering newer data) ---
    # Each save bumps a monotonic rev. If the client posts a client_rev older than what's
    # stored, reject it instead of overwriting good data with stale/default content.
    stored_rev = 0
    try:
        if MODEL_FILE.exists():
            stored_rev = int(json.loads(MODEL_FILE.read_text(encoding="utf-8")).get("rev", 0))
    except Exception:
        stored_rev = 0
    client_rev = data.get("client_rev")
    if client_rev is not None:
        try:
            if int(client_rev) < stored_rev:
                raise StaleWriteError(f"client_rev {client_rev} < stored {stored_rev}")
        except (TypeError, ValueError):
            pass

    clean = {
        "rooms": data.get("rooms") or DEFAULT_MODEL["rooms"],
        "selected_room": data.get("selected_room") or DEFAULT_MODEL["selected_room"]
    }
    # Preserve the whole-house layout migration version so the client only applies the
    # blueprint default positions once and never clobbers the user's manual room drags.
    if "layout_version" in data:
        clean["layout_version"] = data["layout_version"]
    # ALWAYS stamp the recovery marker + bump the revision so seed_version can never go
    # missing (which previously re-armed the restore-on-boot and wiped edits).
    clean["seed_version"] = MODEL_SEED_VERSION
    clean["rev"] = stored_rev + 1
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
                if o.get("type") == "light":
                    if "entity_id" not in o: o["entity_id"] = None
                    # TV + ambient grouping: which TV this light is paired to, and its role
                    if "tv_group" not in o: o["tv_group"] = None
                    if "tv_role" not in o: o["tv_role"] = "ambient"
                if o.get("type") == "tv":
                    # Preview state + ambient sync color for paired lights
                    if "tv_state" not in o: o["tv_state"] = "off"
                    if "sync_color" not in o: o["sync_color"] = "#3b82f6"
    # Safety net: before overwriting, keep the previous version + a small rotation of
    # timestamped backups so a bad save / reset is never total data loss.
    try:
        total_objs = sum(len(r.get("objects", [])) for r in clean.get("rooms", {}).values())
        if MODEL_FILE.exists() and total_objs > 0:
            prev = MODEL_FILE.read_text(encoding='utf-8')
            (DATA_PATH / "model.bak.json").write_text(prev, encoding='utf-8')
            bdir = DATA_PATH / "model_backups"
            bdir.mkdir(exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            (bdir / f"model-{ts}.json").write_text(prev, encoding='utf-8')
            backups = sorted(bdir.glob("model-*.json"))
            for old in backups[:-20]:  # keep the 20 most recent
                try: old.unlink()
                except OSError: pass
    except Exception:
        pass
    write_json(MODEL_FILE, clean)
    return clean["rev"]








# =============================================================================
# Xbox Game Log (persistent recently played)
# =============================================================================
XBOX_LOG_FILE = _seed("xbox_log.json") or (DATA_PATH / "xbox_log.json")

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
LIGHTING_CONFIG_FILE = _seed("lighting_config.json") or (DATA_PATH / "lighting_config.json")

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
ACCESS_REQUESTS_FILE = _seed("access_requests.json") or (DATA_PATH / "access_requests.json")

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
CLUBS_FILE = _seed("clubs.json") or (DATA_PATH / "clubs.json")

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


# Golf rounds — server-side so they survive browser clears and sync across devices
# (they previously lived only in localStorage).
GOLF_ROUNDS_FILE = _seed("golf_rounds.json") or (DATA_PATH / "golf_rounds.json")


def load_golf_rounds():
    if GOLF_ROUNDS_FILE.exists():
        try:
            data = read_json(GOLF_ROUNDS_FILE, [])
            return data if isinstance(data, list) else []
        except Exception:
            pass
    return []


def save_golf_rounds(rounds):
    write_json(GOLF_ROUNDS_FILE, rounds if isinstance(rounds, list) else [])


# Guest access — a server-side switch that lets ANYONE control lights (no password)
# until the admin turns it off or it auto-expires. Enforced at the API, not the UI.
GUEST_ACCESS_FILE = _seed("guest_access.json") or (DATA_PATH / "guest_access.json")


def set_guest_access(enabled: bool, hours: float = 12.0) -> dict:
    from datetime import datetime, timedelta, timezone
    data = {
        "enabled": bool(enabled),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=max(0.1, min(72, hours)))).isoformat() if enabled else None,
    }
    write_json(GUEST_ACCESS_FILE, data)
    return guest_access_status()


def guest_access_status() -> dict:
    """Return whether guest light control is open.

    Auto-expiry is enforced here (server-side): once expires_at passes, status is
    disabled and the file is rewritten so a restart can't accidentally re-open it.
    Guests only ever get light control via require_light_control — never admin.
    """
    from datetime import datetime, timezone
    data = read_json(GUEST_ACCESS_FILE, {})
    if not isinstance(data, dict) or not data.get("enabled"):
        return {"enabled": False, "expires_at": None}
    exp = data.get("expires_at")
    try:
        if exp and datetime.fromisoformat(exp) <= datetime.now(timezone.utc):
            # Persist the closed state so we don't re-read a stale "enabled" forever.
            try:
                write_json(GUEST_ACCESS_FILE, {"enabled": False, "expires_at": None})
            except Exception:
                pass
            return {"enabled": False, "expires_at": None}
    except Exception:
        return {"enabled": False, "expires_at": None}
    return {"enabled": True, "expires_at": exp}


# "Tell the house" command log — every vibe typed, understood or not, so failed
# phrases can be reviewed later and taught to the parser.
VIBE_LOG_FILE = _seed("vibe_log.json") or (DATA_PATH / "vibe_log.json")


def log_vibe(entry: dict):
    log = read_json(VIBE_LOG_FILE, [])
    if not isinstance(log, list):
        log = []
    log.insert(0, entry)
    write_json(VIBE_LOG_FILE, log[:500])   # keep the 500 most recent


def load_vibe_log():
    log = read_json(VIBE_LOG_FILE, [])
    return log if isinstance(log, list) else []


# Ambient light-usage history — one snapshot of every light captured hourly
# during waking hours, so patterns ("game room is always violet after 6pm")
# can be learned later and used to make the house anticipate you.
LIGHT_HISTORY_FILE = _seed("light_history.json") or (DATA_PATH / "light_history.json")


def log_light_snapshot(entry: dict):
    log = read_json(LIGHT_HISTORY_FILE, [])
    if not isinstance(log, list):
        log = []
    log.insert(0, entry)
    write_json(LIGHT_HISTORY_FILE, log[:2500])   # ~7 months of hourly waking-hour snapshots


def load_light_history():
    log = read_json(LIGHT_HISTORY_FILE, [])
    return log if isinstance(log, list) else []


# Daily vitals history — one compact snapshot per calendar day, so we build a
# longitudinal record beyond Oura's rolling window and can mine our own trends.
VITALS_HISTORY_FILE = _seed("vitals_history.json") or (DATA_PATH / "vitals_history.json")


def load_vitals_history():
    items = read_json(VITALS_HISTORY_FILE, [])
    return items if isinstance(items, list) else []


def upsert_vitals_snapshot(entry: dict):
    """Insert or replace the snapshot for entry['day'] (idempotent per day)."""
    day = entry.get("day")
    if not day:
        return
    hist = load_vitals_history()
    hist = [h for h in hist if h.get("day") != day]
    hist.append(entry)
    hist.sort(key=lambda h: h.get("day", ""))
    write_json(VITALS_HISTORY_FILE, hist[-800:])   # ~2+ years of daily snapshots


# Daily Xbox progress history — one snapshot per calendar day of total gamerscore
# and per-game scores, so we can show real momentum (gamerscore earned this week,
# which games you're actually making progress in) instead of a static library.
XBOX_HISTORY_FILE = _seed("xbox_history.json") or (DATA_PATH / "xbox_history.json")


def load_xbox_history():
    items = read_json(XBOX_HISTORY_FILE, [])
    return items if isinstance(items, list) else []


def upsert_xbox_snapshot(entry: dict):
    day = entry.get("day")
    if not day:
        return
    hist = [h for h in load_xbox_history() if h.get("day") != day]
    hist.append(entry)
    hist.sort(key=lambda h: h.get("day", ""))
    write_json(XBOX_HISTORY_FILE, hist[-800:])


# True Xbox play sessions from the server-side observer (start/end/duration) —
# unlike the page-triggered change log, these are real continuous observations.
XBOX_SESSIONS_FILE = _seed("xbox_sessions.json") or (DATA_PATH / "xbox_sessions.json")


def load_xbox_sessions():
    items = read_json(XBOX_SESSIONS_FILE, [])
    return items if isinstance(items, list) else []


def save_xbox_sessions(items):
    write_json(XBOX_SESSIONS_FILE, items if isinstance(items, list) else [])


# Externally-hosted clips (big videos on Cloudflare R2, referenced by URL). The
# server never stores or streams these — it only remembers the link, so R2 keeps
# its free-egress advantage and no R2 credentials ever live on this box.
EXTERNAL_CLIPS_FILE = _seed("clips_external.json") or (DATA_PATH / "clips_external.json")


def load_external_clips():
    items = read_json(EXTERNAL_CLIPS_FILE, [])
    return items if isinstance(items, list) else []


def save_external_clips(items):
    write_json(EXTERNAL_CLIPS_FILE, items if isinstance(items, list) else [])


# =============================================================================
# Blog Message Board (threaded, server-persisted)
# =============================================================================

MESSAGES_FILE = _seed("messages.json") or (DATA_PATH / "messages.json")

LAST_BLOG_READ_FILE = _seed("last_blog_read.json") or (DATA_PATH / "last_blog_read.json")

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

