"""Turn the raw lighting logs (hourly snapshots + change transitions + 'tell the
house' commands) into automation-ready intelligence.

Philosophy: hourly SNAPSHOTS tell us what's on and when (occupancy/ambient
patterns). TRANSITIONS tell us what you deliberately change lights *to*, and at
what time — which is what reveals the scenes you set by hand (your real 'work
mode') and lets us propose automations that match how you actually live. We only
observe; we never invent. Everything here is derived from your own behavior.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from services import persistence

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _hex(rgb):
    if not rgb or len(rgb) < 3:
        return None
    try:
        return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    except Exception:
        return None


def _look_label(look: dict) -> str:
    """A short human name for what a light looks like in a given state."""
    if not look or not look.get("on"):
        return "off"
    if look.get("effect"):
        return str(look["effect"])
    hx = _hex(look.get("rgb"))
    if hx:
        return hx
    if look.get("kelvin"):
        return f"{look['kelvin']}K white"
    return "on"


def _pct(n, d):
    return round(100 * n / d) if d else 0


def _iso_day(ts: str) -> str:
    return (ts or "")[:10]


def _hour_of(ts: str):
    try:
        return int(ts[11:13])
    except Exception:
        return None


def _rows(lights):
    """Snapshot 'lights' is a list of dicts; be defensive about shape."""
    if isinstance(lights, list):
        return lights
    if isinstance(lights, dict):
        return [{"name": k, **(v or {})} for k, v in lights.items()]
    return []


def _looks_raw(name, eid):
    """True if `name` is a fallback rendering of the entity id rather than a
    real friendly name — either the bare id itself, or the id's domain/
    underscore form. Used to pick a clean display name per id even when some
    historical polls only ever logged the id-derived fallback."""
    if not name:
        return True
    if name == eid:
        return True
    bare = (eid or "").split(".", 1)[-1]
    return name.strip().lower() in (bare.lower(), bare.replace("_", " ").lower())


def _resolve_display_names(name_counters: dict) -> dict:
    """id -> best display name, from a Counter of every name string ever seen
    for that id. Prefers the most common REAL friendly name; only falls back
    to a raw/id-derived name if that id never once had a real one recorded.

    This is what merges a light's history back together even though roughly
    18 lights were, before the home_assistant.py friendly_name fallback fix,
    logged under their raw entity_id on the (rare) polls where Home Assistant
    momentarily omitted friendly_name — id is stable and always correct, only
    the name ever forked, so grouping by id here heals every past fork without
    touching a single byte of the stored snapshot/transition files."""
    resolved = {}
    for eid, ctr in name_counters.items():
        clean = [(n, c) for n, c in ctr.items() if not _looks_raw(n, eid)]
        if clean:
            resolved[eid] = max(clean, key=lambda nc: nc[1])[0]
        else:
            top = ctr.most_common(1)
            resolved[eid] = top[0][0] if top else (eid or "?")
    return resolved


def compute_insights():
    snaps = persistence.load_light_history()
    trans = persistence.load_light_transitions()
    vibe = persistence.load_vibe_log()

    # ---- Coverage -----------------------------------------------------------
    snap_days = sorted({_iso_day(s.get("ts", "")) for s in snaps if s.get("ts")})
    # The week-long countdown tracks days with TRANSITION data — that's the new,
    # richer signal that actually powers automation design. Old hourly snapshots
    # are a head start for ambient profiles but can't tell us what you set lights
    # TO, so they don't count toward "ready to build automations".
    trans_days = sorted({_iso_day(t.get("ts", "")) for t in trans if t.get("ts")})
    hours_seen = sorted({_hour_of(s.get("ts", "")) for s in snaps if _hour_of(s.get("ts", "")) is not None})
    coverage = {
        "days_collected": len(trans_days),
        "target_days": 7,
        "progress_pct": min(100, _pct(len(trans_days), 7)),
        "first_day": trans_days[0] if trans_days else None,
        "last_day": trans_days[-1] if trans_days else None,
        "snapshot_days": len(snap_days),
        "snapshots": len(snaps),
        "transitions": len(trans),
        "changes_logged": sum(len(t.get("changes", [])) for t in trans),
        "commands_logged": len(vibe),
        "hours_covered": hours_seen,
        "ready": len(trans_days) >= 7,
    }

    # ---- Per-light profiles (from snapshots) --------------------------------
    # Keyed by entity id (stable, always present) — NOT by name, which is what
    # let a light's history fork in two whenever HA momentarily omitted
    # friendly_name. Display name is resolved once at the end via
    # _resolve_display_names, from every name string ever seen for that id.
    # id -> {on, total, byhour:{h:[on,total]}, looks:Counter, bri:[..]}
    prof = defaultdict(lambda: {"on": 0, "total": 0, "byhour": defaultdict(lambda: [0, 0]),
                                "looks": Counter(), "bri": []})
    id_room = {}
    id_names = defaultdict(Counter)
    for s in snaps:
        h = _hour_of(s.get("ts", ""))
        for lt in _rows(s.get("lights")):
            eid = lt.get("id")
            if not eid:
                continue
            id_names[eid][lt.get("name") or eid] += 1
            if lt.get("room"):
                id_room[eid] = lt.get("room")
            p = prof[eid]
            p["total"] += 1
            on = bool(lt.get("on"))
            if on:
                p["on"] += 1
                p["looks"][_look_label({"on": True, "effect": lt.get("effect"),
                                        "rgb": lt.get("rgb"), "kelvin": lt.get("kelvin")})] += 1
                if lt.get("brightness"):
                    p["bri"].append(int(lt["brightness"]))
            if h is not None:
                cell = p["byhour"][h]
                cell[1] += 1
                cell[0] += on

    # ---- What you SET lights to (from transitions) --------------------------
    # For each light, the target looks you switch it INTO, most common first.
    # Keyed by id for the same reason as the snapshot profiles above; display
    # name resolved from the SAME id_names map so a light's transitions and
    # its snapshot profile always agree on one name, even for entries logged
    # before the friendly_name fallback fix.
    set_to = defaultdict(Counter)
    # Candidate automations: (daytype, hour, light, target) recurring across days.
    auto_key = defaultdict(lambda: {"count": 0, "days": set(), "room": None})
    for t in trans:
        wd = t.get("weekday")
        hr = t.get("hour")
        daytype = "weekday" if isinstance(wd, int) and wd < 5 else "weekend"
        day = _iso_day(t.get("ts", ""))
        for c in t.get("changes", []):
            to = c.get("to") or {}
            if not to.get("on"):
                label = "off"
            else:
                label = _look_label(to)
            eid = c.get("id")
            if not eid:
                continue
            id_names[eid][c.get("name") or eid] += 1
            if c.get("room"):
                id_room[eid] = c.get("room")
            set_to[eid][label] += 1
            if hr is not None:
                k = (daytype, hr, eid, label)
                a = auto_key[k]
                a["count"] += 1
                a["days"].add(day)
                a["room"] = c.get("room") or a["room"]

    # Resolve display names ONCE, after both snapshot and transition names have
    # fed id_names — so a light's profile and its transitions always agree on
    # the same name, including entries logged before the friendly_name
    # fallback fix (see home_assistant.capture_light_snapshot).
    display_name = _resolve_display_names(id_names)

    lights = []
    for eid, p in prof.items():
        fav = p["looks"].most_common(1)
        byhour = {h: _pct(v[0], v[1]) for h, v in sorted(p["byhour"].items())}
        peak = max(byhour.items(), key=lambda kv: kv[1]) if byhour else None
        lights.append({
            "name": display_name.get(eid, eid),
            "room": id_room.get(eid),
            "on_rate": _pct(p["on"], p["total"]),
            "samples": p["total"],
            "favorite_look": fav[0][0] if fav else None,
            "favorite_look_pct": _pct(fav[0][1], p["on"]) if fav and p["on"] else 0,
            "typical_brightness": round(sum(p["bri"]) / len(p["bri"]) / 255 * 100) if p["bri"] else None,
            "byhour": byhour,
            "peak_hour": peak[0] if peak else None,
            "look_mix": p["looks"].most_common(4),
        })
    # Drop non-informative entities from display: distinct from the name-fork
    # bug above, some real HA entities (WLED's per-segment sub-lights: Main,
    # Segment 1-4) have their own genuine, correct entity id but never report
    # an "on" state — they're wiring noise, not a light anyone actually uses.
    # Data isn't touched, just not surfaced in a list meant to drive automations.
    lights = [l for l in lights if l["on_rate"] > 0 or l["favorite_look"]]
    lights.sort(key=lambda l: l["on_rate"], reverse=True)

    set_preferences = []
    for eid, ctr in set_to.items():
        top = ctr.most_common(3)
        set_preferences.append({
            "name": display_name.get(eid, eid),
            "changes": sum(ctr.values()),
            "top_targets": [{"look": lbl, "count": n, "pct": _pct(n, sum(ctr.values()))} for lbl, n in top],
        })
    set_preferences.sort(key=lambda x: x["changes"], reverse=True)

    # Rank candidate automations by how repeatable they are (distinct days matter
    # more than raw count — 3 different days beats 3 hits in one evening).
    candidates = []
    for (daytype, hr, eid, label), a in auto_key.items():
        ndays = len(a["days"])
        if a["count"] < 2:
            continue
        name = display_name.get(eid, eid)
        strength = "strong" if ndays >= 3 else ("emerging" if ndays >= 2 else "weak")
        h12 = (hr % 12) or 12
        ap = "AM" if hr < 12 else "PM"
        candidates.append({
            "daytype": daytype,
            "hour": hr,
            "when": f"{daytype}s around {h12}{ap}",
            "light": name,
            "room": a["room"],
            "target": label,
            "count": a["count"],
            "days": ndays,
            "strength": strength,
            "suggestion": f"On {daytype}s around {h12}{ap}, set {name} → {label}",
        })
    order = {"strong": 0, "emerging": 1, "weak": 2}
    candidates.sort(key=lambda c: (order[c["strength"]], -c["days"], -c["count"]))

    # ---- Vibe log: scenes you asked for that don't exist --------------------
    wanted = [{"text": e.get("text"), "ts": e.get("ts")} for e in vibe if not e.get("understood")]
    room_requests = Counter()
    for e in vibe:
        interp = (e.get("interpretation") or "")
        if "→" in interp:
            room_requests[interp.split("→")[0].strip()] += 1

    return {
        "coverage": coverage,
        "lights": lights,
        "set_preferences": set_preferences,
        "candidate_automations": candidates[:25],
        "unmet_requests": wanted,
        "busiest_rooms_by_command": room_requests.most_common(8),
    }
