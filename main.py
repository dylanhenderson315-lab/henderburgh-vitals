"""HENDER VITALS Dashboard — FastAPI + HTMX + Tailwind + Chart.js"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Body, FastAPI, File, Form, Request, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import (
    clear_admin_session_cookie,
    create_admin_session,
    invalidate_admin_session,
    is_admin_authenticated,
    require_admin,
    set_admin_session_cookie,
)
from clients.oura import OuraClient, _safe_get, get_date_range, process_dashboard_data, time_greeting_now
from config import (
    ADMIN_TOKEN,
    AUTO_REFRESH_SECONDS,
    DISPLAY_NAME,
    HA_ENABLED,
    OURA_DAYS,
    OURA_TOKEN,
    PORT,
    PUBLIC_MODE,
    SITE_NAME,
    SITE_URL,
)
from rate_limit import RateLimitMiddleware, rate_limiter
from services import home_assistant, persistence, state, vitals, xbox
from services.xbox import fetch_xbox_status

templates = Jinja2Templates(directory="templates")


async def _light_history_loop():
    """Once per hour during waking hours (10am–8pm ET), snapshot every light so
    usage patterns can be learned later — same 'log now, mine later' idea as the
    command log. Sleeps to the top of each hour; skips logging when HA is down."""
    tz = ZoneInfo("America/New_York")
    while True:
        now = datetime.now(tz)
        nxt = (now + timedelta(hours=1)).replace(minute=0, second=5, microsecond=0)
        try:
            await asyncio.sleep(max(30, (nxt - now).total_seconds()))
        except asyncio.CancelledError:
            raise
        try:
            hour = datetime.now(tz).hour
            if 10 <= hour <= 20:
                snap = await home_assistant.capture_light_snapshot()
                if snap:
                    snap["ts"] = datetime.now(tz).isoformat()
                    persistence.log_light_snapshot(snap)
        except Exception as e:
            print(f"light history snapshot error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if OURA_TOKEN:
        state.oura_client = OuraClient(OURA_TOKEN)
    elif PUBLIC_MODE:
        raise RuntimeError("PUBLIC_MODE requires OURA_TOKEN")
    history_task = asyncio.create_task(_light_history_loop())
    yield
    history_task.cancel()
    if state.oura_client:
        await state.oura_client.close()
        state.oura_client = None


app = FastAPI(title="HENDERBURGH", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(RateLimitMiddleware)
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


def _render(template_name: str, context: Dict[str, Any]) -> HTMLResponse:
    template = templates.env.get_template(template_name)
    return HTMLResponse(template.render(**context))


def client_ip(request: Request) -> str:
    """Real visitor IP behind Cloudflare + Railway.

    The site is proxied by Cloudflare, so request.client.host (and often the first
    X-Forwarded-For hop) is a Cloudflare edge IP — not the visitor. Cloudflare puts the
    true client IP in CF-Connecting-IP (and True-Client-IP on Enterprise). Prefer those,
    then fall back to the first X-Forwarded-For entry, then the raw socket.
    """
    for header in ("cf-connecting-ip", "true-client-ip"):
        val = (request.headers.get(header) or "").strip()
        if val:
            return val
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


# Failed unlock attempts per IP (in-memory), for brute-force protection on the admin PIN.
_UNLOCK_FAILS: Dict[str, list] = {}


@app.post("/api/auth/unlock")
async def auth_unlock(request: Request, body: dict = Body(...)):
    import hmac as _hmac
    import time as _time

    ip = client_ip(request)
    now = _time.time()
    window, max_fails, lock_secs = 900, 5, 900  # 5 tries / 15 min, then 15-min lockout

    fails = [t for t in _UNLOCK_FAILS.get(ip, []) if now - t < window]
    if len(fails) >= max_fails:
        # Brute-force protection: makes a short PIN safe (5 guesses per 15 min).
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    token = (body.get("token") or "").strip()
    # constant-time compare to avoid timing leaks
    ok = bool(ADMIN_TOKEN) and _hmac.compare_digest(token, ADMIN_TOKEN)
    if not ok:
        fails.append(now)
        _UNLOCK_FAILS[ip] = fails
        remaining = max(0, max_fails - len(fails))
        raise HTTPException(status_code=403, detail=f"Invalid admin token ({remaining} attempts left)")

    _UNLOCK_FAILS.pop(ip, None)  # reset on success
    session_id = create_admin_session()
    response = JSONResponse({"status": "ok", "unlocked": True})
    set_admin_session_cookie(response, session_id)
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    invalidate_admin_session(request.cookies.get("admin_session"))
    response = JSONResponse({"status": "ok", "unlocked": False})
    clear_admin_session_cookie(response)
    return response


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """Honest access picture for the lighting UI.

    - unlocked: admin password session (cookie)
    - guest: house-wide light control is open (no password)
    - can_control: either of the above — this is what the UI should use for toggles
    """
    guest = persistence.guest_access_status()
    admin = is_admin_authenticated(request)
    guest_on = bool(guest.get("enabled"))
    return {
        "unlocked": admin,
        "configured": bool(ADMIN_TOKEN),
        "guest": guest_on,
        "guest_expires": guest.get("expires_at"),
        "can_control": admin or guest_on,
    }


def require_light_control(request: Request) -> None:
    """Light control = admin session OR guest access switched on by the admin.
    Guest access grants ONLY this — never model editing, backups, or admin panels."""
    if is_admin_authenticated(request):
        return
    if persistence.guest_access_status()["enabled"]:
        return
    raise HTTPException(status_code=403, detail="Controls are locked")


@app.get("/api/ha/guest-access")
async def get_guest_access():
    """Public read: is the house currently open to guests? (UI needs the honest state.)"""
    return persistence.guest_access_status()


@app.post("/api/ha/guest-access")
async def post_guest_access(request: Request, body: dict = Body(default={})):
    """Admin only: open/close guest light control. Auto-expires (default 12h, max 72h)."""
    require_admin(request)
    enabled = bool(body.get("enabled"))
    hours = float(body.get("hours") or 12)
    return persistence.set_guest_access(enabled, hours)


async def fetch_all_data(days: int = OURA_DAYS):
    return await vitals.fetch_all_data(days)


async def get_current_steps():
    return await vitals.get_current_steps()


async def get_current_heart_rate():
    return await vitals.get_current_heart_rate()

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Clean minimalist home page for HENDERBURGH."""
    steps = None
    latest_hr = None
    hr_age_minutes = None
    latest_hr_timestamp = None

    metrics = vitals.snapshot_home_metrics()
    if state.oura_client and metrics.get("steps") is None:
        try:
            days = 14 if PUBLIC_MODE else OURA_DAYS
            processed = await vitals.get_processed_vitals(days=days, use_cache=True)
            metrics = {
                "steps": processed.get("steps"),
                "latest_hr": processed.get("latest_hr"),
                "hr_age_minutes": processed.get("hr_age_minutes"),
                "latest_hr_timestamp": processed.get("latest_hr_timestamp"),
            }
        except Exception:
            pass
    steps = metrics.get("steps")
    latest_hr = metrics.get("latest_hr")
    hr_age_minutes = metrics.get("hr_age_minutes")
    latest_hr_timestamp = metrics.get("latest_hr_timestamp")

    # Prepare context for homepage (same values as vitals page)
    steps_ctx = {
        "count": steps,
        "miles": round(steps * 0.0005, 1) if steps is not None else None,
    }

    # Format "updated X ago" the same way as vitals
    hr_updated_ago = "recently"
    if latest_hr_timestamp and hr_age_minutes is not None:
        try:
            ts = datetime.fromisoformat(latest_hr_timestamp.replace("Z", "+00:00"))
            local_tz = ZoneInfo("America/New_York")
            local_time = ts.astimezone(local_tz)

            if hr_age_minutes < 60:
                hr_updated_ago = f"updated {hr_age_minutes}m ago"
            else:
                hr_updated_ago = local_time.strftime("updated at %-I:%M %p").replace(" 0", " ")
        except Exception:
            hr_updated_ago = "recently"

    hr_ctx = {
        "bpm": latest_hr,
        "updated_ago": hr_updated_ago,
    }

    # Blog content for the home hub: recent top-level posts (neat teaser) + unread count for the lamp poke notifier.
    # We surface actual blog activity directly on the main screen as first-class content.
    recent_messages = []
    blog_unread_count = 0
    try:
        all_messages = persistence.load_messages()

        # Unread (for the quirky office-lamp poke icon) — counts replies too
        last_read = persistence.load_last_blog_read()
        unread_messages = [m for m in all_messages if str(m.get("timestamp", "")) > last_read]
        blog_unread_count = len(unread_messages)

        # Teaser for main screen: only top-level "posts", sorted newest first, last 3.
        # Enrich with a clean display_date so the UI stays pretty without JS.
        top_level = [m for m in all_messages if not m.get("parent_id")]
        top_level.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
        recent_messages = top_level[:3]

        for m in recent_messages:
            ts = str(m.get("timestamp", ""))
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                m["display_date"] = dt.strftime("%b %-d")
            except Exception:
                m["display_date"] = ts[:10] if ts else "—"
    except Exception:
        recent_messages = []
        blog_unread_count = 0

    # Real clips only — never placeholders (empty list hides dead players on home).
    recent_clips = list_clips(limit=6)

    return _render("home.html", {
        "request": request,
        "public_mode": PUBLIC_MODE,
        "site_name": SITE_NAME,
        "display_name": DISPLAY_NAME,
        "steps": steps_ctx,
        "heart_rate": hr_ctx,
        "recent_messages": recent_messages,
        "blog_unread_count": blog_unread_count,
        "recent_clips": recent_clips,
        "time_greeting": time_greeting_now(),
    })




@app.get("/xbox", response_class=HTMLResponse)
async def xbox_page(request: Request, background_tasks: BackgroundTasks = None):
    """Dedicated Xbox profile page with current data and game log.
    Stale-while-revalidate: if we have any last-known-good data, render instantly
    with it and refresh from xbl.io in the background (was blocking ~1.5s)."""
    if state.last_xbox_data.get("status") == "ok":
        xbox_data = state.last_xbox_data
        if background_tasks is not None:
            background_tasks.add_task(fetch_xbox_status)
    else:
        xbox_data = await fetch_xbox_status()
    raw_log = persistence.load_xbox_log()

    # Enrich log for clean, professional display (formatted dates, safe fields)
    formatted_log = []
    for entry in raw_log:
        ts = entry.get("timestamp", "")
        played = ts
        try:
            # Support both naive ISO and Z-suffixed
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            # e.g. "Feb 3, 2026 • 2:14 PM"
            played = dt.strftime("%b %-d, %Y • %-I:%M %p")
        except Exception:
            pass
        formatted_log.append({
            "game": entry.get("game", "—"),
            "timestamp": ts,
            "played": played,
            "device": entry.get("device", "") or "",
        })

    # Add a few display helpers to xbox data for the template (no mutation of cache)
    xbox_display = dict(xbox_data) if xbox_data else {}
    try:
        gs = int(xbox_display.get("gamerscore") or 0)
        xbox_display["gamerscore_display"] = f"{gs:,}"
    except Exception:
        xbox_display["gamerscore_display"] = xbox_display.get("gamerscore") or "0"

    return _render("xbox.html", {
        "request": request,
        "xbox": xbox_display,
        "log": formatted_log,
        "public_mode": PUBLIC_MODE,
        "site_name": SITE_NAME,
        "display_name": DISPLAY_NAME,
    })




@app.get("/home-assistant", response_class=HTMLResponse)
async def ha_page(request: Request):
    """Private Home Assistant control page. Controls require admin token (see blog pattern).
    In PUBLIC_MODE the page renders but shows disabled state and no controls.
    """
    # Fast path: we no longer block the entire page on full HA states for the lighting UI.
    # entity_count is best-effort (falls back to last snapshot or 0). The real lighting data
    # (rooms/groups + live states) is delivered instantly via embedded INITIAL_LIGHTING + background /lights-data.
    states = []
    try:
        if HA_ENABLED:
            # Lightweight: still useful for header count, but do not let a slow/401 HA kill the instant feel.
            states = await home_assistant.get_ha_states()
    except Exception:
        states = []

    # The legacy "groups" + ordered domain list is only used for the disabled-state message path.
    # Keep the computation but it is cheap on the (usually empty or small) states list.
    groups: Dict[str, List[Dict]] = {}
    for s in states:
        eid = s.get("entity_id", "unknown.unknown")
        domain = eid.split(".", 1)[0] if "." in eid else "other"
        if domain in ("sensor", "binary_sensor", "sun", "zone", "device_tracker", "weather"):
            continue
        groups.setdefault(domain, []).append(s)

    for dom in groups:
        groups[dom].sort(key=lambda e: (e.get("attributes", {}).get("friendly_name") or e.get("entity_id")).lower())

    domain_order = ["light", "scene", "switch", "media_player", "input_boolean", "automation", "script", "cover", "fan", "other"]
    ordered_groups = []
    for d in domain_order:
        if d in groups:
            ordered_groups.append((d, groups.pop(d)))
    for d, ents in sorted(groups.items()):
        ordered_groups.append((d, ents))

    # Exact persisted rooms + groups from lighting_config.json — this is what makes the UI feel instant.
    initial_lighting = persistence.load_lighting_config() if HA_ENABLED else {"rooms": [], "groups": []}

    # Prefer a fast count from last snapshot when available
    snap_total = (state.last_ha_lights_snapshot.get("data") or {}).get("total") or 0
    display_count = len(states) or snap_total or (len(initial_lighting.get("rooms", [])) * 2)  # rough but never blocks

    # Bake auth/guest into the first paint so Guest Mode is correct even before JS fetches status.
    guest = persistence.guest_access_status() if HA_ENABLED else {"enabled": False, "expires_at": None}
    admin = is_admin_authenticated(request) if HA_ENABLED else False
    initial_auth = {
        "unlocked": bool(admin),
        "configured": bool(ADMIN_TOKEN),
        "guest": bool(guest.get("enabled")),
        "guest_expires": guest.get("expires_at"),
        "can_control": bool(admin) or bool(guest.get("enabled")),
    }

    return _render("home-assistant.html", {
        "request": request,
        "groups": ordered_groups,
        "entity_count": display_count,
        "ha_enabled": HA_ENABLED,
        "public_mode": PUBLIC_MODE,
        "has_admin_token": bool(ADMIN_TOKEN),
        "site_name": SITE_NAME,
        "initial_lighting": initial_lighting,
        "initial_auth": initial_auth,
        "display_name": DISPLAY_NAME,
    })




@app.get("/vitals", response_class=HTMLResponse)
async def vitals_dashboard(request: Request, days: int = OURA_DAYS):
    """The Oura Ring vitals dashboard (moved to /vitals per site structure)."""
    # Force clean URLs in public mode — redirect away from any ?days= param
    if PUBLIC_MODE and request.query_params.get("days"):
        return RedirectResponse(url="/vitals", status_code=302)

    # Always use fixed 14-day window in public mode
    if PUBLIC_MODE:
        days = 14
    # Basic rate limiting in public mode
    if PUBLIC_MODE:
        if not rate_limiter.is_allowed(client_ip(request)):
            return HTMLResponse(
                "<div style='font-family:sans-serif;padding:40px;text-align:center;color:#666'>"
                "Too many requests. Please slow down.</div>",
                status_code=429
            )

    # In public mode we never show the setup screen
    if not OURA_TOKEN or not state.oura_client:
        if PUBLIC_MODE:
            return HTMLResponse(
                "<h1 style='font-family:sans-serif;color:#111'>Configuration error</h1>"
                "<p>Dashboard is not properly configured.</p>",
                status_code=500
            )
        return _render(
            "dashboard.html",
            {
                "request": request,
                "setup_mode": True,
                "has_token": bool(OURA_TOKEN),
                "display_name": DISPLAY_NAME,
                "hr_age_minutes": None,
                "recent_activities": [],
            },
        )

    try:
        ctx = await vitals.get_processed_vitals(days, use_cache=False)

        ctx.update({
            "request": request,
            "setup_mode": False,
            "error": None,
            "public_mode": PUBLIC_MODE,
            "display_name": DISPLAY_NAME,
            "site_name": SITE_NAME,
            "site_url": SITE_URL,
            "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
        })
        return _render("dashboard.html", ctx)
    except Exception as e:
        if PUBLIC_MODE:
            # In public mode, show a clean error instead of exposing internals
            return _render(
                "dashboard.html",
                {
                    "request": request,
                    "setup_mode": False,
                    "error": "Unable to load latest data right now. Please check back soon.",
                    "public_mode": True,
                    "display_name": DISPLAY_NAME,
                    "site_name": SITE_NAME,
                    "site_url": SITE_URL,
                    "name": DISPLAY_NAME,
                    "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
                    "hr_age_minutes": None,
                    "recent_activities": [],
                    "last_updated_iso": None,
                },
            )
        return _render(
            "dashboard.html",
            {
                "request": request,
                "setup_mode": False,
                "error": str(e),
                "name": "there",
                "display_name": DISPLAY_NAME,
                "hr_age_minutes": None,
                "time_greeting": "HENDERBURGH IS HERE.",
                "recent_activities": [],
                "last_updated_iso": None,
            },
        )




@app.get("/fragment", response_class=HTMLResponse)
async def dashboard_fragment(request: Request, days: int = OURA_DAYS):
    """Legacy fragment endpoint (still works for the vitals dashboard)."""
    return await vitals_fragment(request, days)




@app.get("/vitals/fragment", response_class=HTMLResponse)
async def vitals_fragment(request: Request, days: int = OURA_DAYS):
    # Always ignore ?days= in public mode for clean URLs
    if PUBLIC_MODE:
        days = 14
    """HTMX target — disabled in public mode to protect API quota."""
    if PUBLIC_MODE:
        return HTMLResponse(
            "<div class='text-sm text-zinc-400 p-4'>Data automatically refreshes every ~10 minutes.</div>",
            200
        )

    if not OURA_TOKEN or not state.oura_client:
        return HTMLResponse("<div class='text-red-400 p-8'>Token not configured</div>", 400)

    try:
        ctx = await vitals.get_processed_vitals(days, use_cache=False)
        ctx.update({
            "request": request,
            "setup_mode": False,
            "error": None,
            "fragment": True,
            "auto_refresh_seconds": AUTO_REFRESH_SECONDS,
        })
        return _render("dashboard.html", ctx)
    except Exception as e:
        return HTMLResponse(f"<div class='text-red-400 p-8'>Error: {e}</div>", 500)




@app.get("/api/latest-hr")
async def api_latest_hr(fresh: bool = False):
    """Lightweight endpoint for fast Heart Rate updates.
    Use ?fresh=true for the live modal (bypasses cache for freshest possible data).
    """
    if not state.oura_client:
        return JSONResponse({"bpm": None, "timestamp": None})

    try:
        now = datetime.now(timezone.utc)

        if fresh:
            # Very recent window for true "live" feel in the modal
            start_dt = (now - timedelta(minutes=30)).isoformat()
            end_dt = now.isoformat()

            hr_data = await state.oura_client._get(
                "/usercollection/heartrate",
                {"start_datetime": start_dt, "end_datetime": end_dt},
                bypass_cache=True   # Force fresh data from Oura
            )
        else:
            # Normal cached call (used by the regular 2-min background refresh)
            start_dt = (now - timedelta(hours=4)).isoformat()
            end_dt = now.isoformat()

            hr_data = await state.oura_client._get(
                "/usercollection/heartrate",
                {"start_datetime": start_dt, "end_datetime": end_dt},
                bypass_cache=False
            )

        hr_data = hr_data.get("data", []) if isinstance(hr_data, dict) else []

        latest_hr = None
        latest_hr_timestamp = None

        if hr_data:
            for entry in reversed(hr_data):
                if isinstance(entry, dict) and entry.get("bpm") is not None:
                    latest_hr = entry.get("bpm")
                    latest_hr_timestamp = entry.get("timestamp")
                    break

        if latest_hr is None:
            # Fallback
            recent_start = (now - timedelta(days=1)).date().isoformat()
            detailed = await state.oura_client.get_sleep(recent_start, now.date().isoformat())
            if detailed:
                latest_detailed = detailed[-1] if detailed else None
                latest_hr = _safe_get(latest_detailed, "average_heart_rate") or _safe_get(latest_detailed, "lowest_heart_rate")

        # Compute age for the response
        age_minutes = None
        if latest_hr_timestamp:
            try:
                ts = datetime.fromisoformat(latest_hr_timestamp.replace("Z", "+00:00"))
                age_minutes = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
            except Exception:
                pass

        return JSONResponse({
            "bpm": int(latest_hr) if latest_hr is not None else None,
            "timestamp": latest_hr_timestamp,
            "age_minutes": age_minutes
        })
    except Exception:
        return JSONResponse({"bpm": None, "timestamp": None})




@app.get("/health")
async def health():
    """Health check endpoint for Railway / monitoring (does not hit Oura API)."""
    # Storage diagnostics: confirm whether model edits persist (volume) and how many
    # objects are currently saved — used to verify the persistence fix is working.
    import os as _os
    from services import persistence as _p
    try:
        _m = _p.load_model()
        _objs = sum(len(r.get("objects", [])) for r in _m.get("rooms", {}).values())
        _sv = _m.get("seed_version")
    except Exception:
        _objs, _sv = None, None
    return {
        "status": "ok",
        "public_mode": PUBLIC_MODE,
        "has_token": bool(OURA_TOKEN),
        "display_name": DISPLAY_NAME,
        "site": SITE_NAME,
        "data_dir": _os.getenv("DATA_DIR", "data"),
        "data_dir_is_volume": _os.getenv("DATA_DIR", "data") not in ("data", "./data"),
        "model_objects": _objs,
        "model_seed_version": _sv,
    }


async def get_current_steps():
    """Internal helper to get current steps and miles (same logic as vitals page)."""
    if not state.oura_client:
        return {"steps": None, "miles": None}

    try:
        today = date.today().isoformat()
        activity = await state.oura_client.get_daily_activity(today, today)
        if activity and len(activity) > 0:
            latest = activity[0]
            steps = latest.get("steps") or 0
            miles = round(steps * 0.0005, 1) if steps else 0
            return {"steps": steps, "miles": miles}
    except Exception:
        pass
    return {"steps": None, "miles": None}




@app.get("/api/steps")
async def api_steps():
    """Lightweight endpoint for current steps (used on homepage Live Now)."""
    return await get_current_steps()


async def get_current_heart_rate():
    """Internal helper to get current heart rate (same logic used by vitals page)."""
    if not state.oura_client:
        return {"bpm": None, "updated": None}

    try:
        now = datetime.now(timezone.utc)
        # Get last ~4 hours of HR samples
        start_dt = (now - timedelta(hours=4)).isoformat()
        hr_data = await state.oura_client._get(
            "/usercollection/heartrate",
            {"start_datetime": start_dt, "end_datetime": now.isoformat()},
            bypass_cache=True
        )
        hr_list = hr_data.get("data", []) if isinstance(hr_data, dict) else []

        if hr_list:
            latest = hr_list[-1]
            bpm = latest.get("bpm")
            ts = latest.get("timestamp")
            return {
                "bpm": bpm,
                "updated": ts
            }
    except Exception:
        pass

    return {"bpm": None, "updated": None}




@app.get("/api/heart-rate")
async def api_heart_rate():
    """Lightweight endpoint for current heart rate (used on homepage Live Now)."""
    return await get_current_heart_rate()




@app.get("/api/xbox/status")
async def api_xbox_status():
    return await fetch_xbox_status()


@app.get("/api/ha/summary")
async def api_ha_summary():
    """Lightweight summary used by the homepage Live Now card. Always safe to call."""
    return await home_assistant.get_ha_summary()




@app.get("/api/ha/states")
async def api_ha_states():
    """Full entity list for the /home-assistant control UI (read-only)."""
    if not HA_ENABLED:
        return {"status": "disabled", "entities": []}
    states = await home_assistant.get_ha_states()
    return {"status": "ok", "entities": states}




@app.get("/api/ha/lights-data")
async def api_ha_lights_data():
    """Rich data for the professional lighting dashboard: custom rooms, groups, lights, scenes, unassigned.

    Public read (view-only). Control still gated by require_light_control (admin OR guest mode).
    When HA is unreachable, payload may include ha_ok=false with last-known / config structure
    so the UI does not collapse to empty rooms.
    """
    if not HA_ENABLED:
        return {"status": "disabled", "ha_ok": False, "rooms": [], "groups": [], "lights_by_room": {}, "unassigned_lights": [], "scenes": [], "total_lights": 0, "sync_controls": []}
    data = await home_assistant.get_ha_lights_data()
    status = "ok" if data.get("ha_ok", True) else "degraded"
    return {"status": status, **data}




@app.get("/api/ha/bootstrap")
async def api_ha_bootstrap():
    """Ultra-fast (no HA roundtrip) bootstrap for instant UI.
    Returns exact persisted rooms + Sync Groups from lighting_config.json + last known light states (if any).
    Frontend renders the full structure + skeleton/last-known cards immediately, then background-refreshes live states.
    """
    if not HA_ENABLED:
        return {"status": "disabled", "rooms": [], "groups": [], "last_states": {}}
    struct = persistence.get_persisted_lighting_structure()
    return {"status": "ok", **struct}


# --- Lighting management (rooms, groups, assignments) - protected by ADMIN_TOKEN ---



@app.post("/api/ha/lighting/room/create")
async def create_room(request: Request, name: str = ""):
    require_admin(request)
    if not name.strip():
        raise HTTPException(400, "Room name required")
    config = persistence.load_lighting_config()
    new_room = {"id": str(uuid.uuid4()), "name": name.strip(), "light_ids": []}
    config["rooms"].append(new_room)
    persistence.save_lighting_config(config)
    return {"status": "ok", "room": new_room}




@app.post("/api/ha/lighting/room/rename")
async def rename_room(request: Request, room_id: str = "", name: str = ""):
    require_admin(request)
    config = persistence.load_lighting_config()
    for room in config["rooms"]:
        if room["id"] == room_id:
            room["name"] = name.strip()
            persistence.save_lighting_config(config)
            return {"status": "ok"}
    raise HTTPException(404, "Room not found")




@app.post("/api/ha/lighting/room/delete")
async def delete_room(request: Request, room_id: str = ""):
    require_admin(request)
    config = persistence.load_lighting_config()
    config["rooms"] = [r for r in config["rooms"] if r["id"] != room_id]
    persistence.save_lighting_config(config)
    return {"status": "ok"}




@app.post("/api/ha/lighting/assign")
async def assign_light_to_room(request: Request, entity_id: str = "", room_id: str = ""):
    """Assign (move) a light to a room. Pass room_id="" to unassign."""
    require_admin(request)
    config = persistence.load_lighting_config()
    # Remove from all rooms first
    for room in config["rooms"]:
        if entity_id in room.get("light_ids", []):
            room["light_ids"].remove(entity_id)
    if room_id:
        for room in config["rooms"]:
            if room["id"] == room_id:
                if entity_id not in room.get("light_ids", []):
                    room.setdefault("light_ids", []).append(entity_id)
                break
    persistence.save_lighting_config(config)
    return {"status": "ok"}




@app.post("/api/ha/lighting/group/create")
async def create_group(request: Request, name: str = ""):
    require_admin(request)
    if not name.strip():
        raise HTTPException(400, "Group name required")
    config = persistence.load_lighting_config()
    new_group = {"id": str(uuid.uuid4()), "name": name.strip(), "light_ids": []}
    config["groups"].append(new_group)
    persistence.save_lighting_config(config)
    return {"status": "ok", "group": new_group}




@app.post("/api/ha/lighting/group/delete")
async def delete_group(request: Request, group_id: str = ""):
    require_admin(request)
    config = persistence.load_lighting_config()
    config["groups"] = [g for g in config["groups"] if g["id"] != group_id]
    persistence.save_lighting_config(config)
    return {"status": "ok"}




@app.post("/api/ha/lighting/group/assign")
async def assign_light_to_group(request: Request, entity_id: str = "", group_id: str = "", add: bool = True):
    require_admin(request)
    config = persistence.load_lighting_config()
    for group in config["groups"]:
        if group["id"] == group_id:
            if add:
                if entity_id not in group.get("light_ids", []):
                    group.setdefault("light_ids", []).append(entity_id)
            else:
                if entity_id in group.get("light_ids", []):
                    group["light_ids"].remove(entity_id)
            persistence.save_lighting_config(config)
            return {"status": "ok"}
    raise HTTPException(404, "Group not found")




@app.post("/api/ha/lighting/group/rename")
async def rename_group(request: Request, group_id: str = "", name: str = ""):
    require_admin(request)
    config = persistence.load_lighting_config()
    for group in config.get("groups", []):
        if group["id"] == group_id:
            group["name"] = name.strip()
            persistence.save_lighting_config(config)
            return {"status": "ok"}
    raise HTTPException(404, "Group not found")




@app.post("/api/ha/service/{domain}/{service}")
async def api_ha_service(request: Request, 
    domain: str,
    service: str,
        payload: dict = Body(default={}),
):
    """Generic service caller with rich payload support.
    Light control gate: admin session OR guest access (admin-granted, auto-expiring).
    payload can include: entity_id, brightness (0-255), color_temp (mireds), rgb_color: [r,g,b], etc.
    Used by the professional lighting dashboard.
    """
    require_light_control(request)
    if PUBLIC_MODE or not HA_ENABLED:
        raise HTTPException(status_code=403, detail="Home Assistant controls are disabled in public mode")

    entity_id = payload.get("entity_id")
    extra = {k: v for k, v in payload.items() if k != "entity_id"}

    result = await home_assistant.call_ha_service(domain, service, entity_id, extra)
    return {"status": "ok", "domain": domain, "service": service, "entity_id": entity_id, "result": result}




@app.post("/api/ha/poke")
async def api_ha_poke(request: Request, 
        background_tasks: BackgroundTasks = None,
):
    """Poke action: instantaneous blink (off ~0.25s then back on). Backend handled.
    Public on purpose — anyone (e.g. a friend) can poke the lamp. Rate limited so it
    can't be spammed. Only blinks the office lamp; no other control is exposed.
    """
    if PUBLIC_MODE or not HA_ENABLED:
        raise HTTPException(status_code=403, detail="Home Assistant controls are disabled in public mode")

    now = time.time()
    if now - state._poke_last < 3.0:
        raise HTTPException(status_code=429, detail="Rate limited. Wait 3 seconds between pokes.")
    state._poke_last = now

    if background_tasks is not None:
        background_tasks.add_task(home_assistant.perform_poke_blink)
    else:
        asyncio.create_task(home_assistant.perform_poke_blink())
    return {"status": "ok", "action": "poke"}




@app.post("/api/ha/notify")
async def api_ha_notify(request: Request, 
        background_tasks: BackgroundTasks = None,
):
    """Notification action: instantaneous blue flash (~0.25s) then restore to previous.
    (Same light + quick flash behavior as used automatically for new blog posts.)
    Backend handled reliably. Returns instantly. (Rate limited.)
    """
    require_admin(request)
    if PUBLIC_MODE or not HA_ENABLED:
        raise HTTPException(status_code=403, detail="Home Assistant controls are disabled in public mode")

    now = time.time()
    if now - state._notify_last < 5.0:
        raise HTTPException(status_code=429, detail="Rate limited.")
    state._notify_last = now

    if background_tasks is not None:
        background_tasks.add_task(home_assistant.perform_notify_blue)
    else:
        asyncio.create_task(home_assistant.perform_notify_blue())
    return {"status": "ok", "action": "notify"}




@app.post("/api/ha/access-request")
async def post_access_request(request: Request, name: str = Form(""), message: str = Form("")):
    """Anyone can submit an access request when the lighting page is locked.
    No admin token required. Logs basic info + user message.
    """
    if not message or not message.strip():
        raise HTTPException(400, "Message is required")

    # Real visitor IP via Cloudflare's CF-Connecting-IP (was logging Cloudflare edge IPs)
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "unknown")
    ts = datetime.now(timezone.utc).isoformat()

    req = {
        "id": str(uuid.uuid4())[:8],
        "name": (name or "Anonymous").strip(),
        "message": message.strip(),
        "ip": ip,
        "user_agent": ua,
        "timestamp": ts
    }

    reqs = persistence.load_access_requests()
    reqs.insert(0, req)  # newest first
    persistence.save_access_requests(reqs)
    return {"status": "received"}




@app.get("/api/ha/access-requests")
async def get_access_requests(request: Request):
    """Protected: list access requests (only when page is unlocked with valid token)."""
    require_admin(request)
    return persistence.load_access_requests()




@app.get("/api/ha/tv-sync")
async def get_tv_sync():
    """Public read for TV Sync config (assignments/mode/settings). No token required for viewing the UI."""
    return persistence.load_tv_sync()




@app.get("/api/model")
async def get_model():
    """Public read for model data (rooms, positions, heights, modes, furniture)."""
    return persistence.load_model()




@app.post("/api/model")
async def post_model(request: Request, data: dict):
    """Save model data. Admin only — the model is locked so visitors can't alter it.
    Rejects stale writes (an old tab whose client_rev is behind) to prevent data loss."""
    require_admin(request)
    try:
        rev = persistence.save_model(data)
    except persistence.StaleWriteError:
        raise HTTPException(status_code=409, detail="Stale save: this page is out of date. Reload before editing.")
    return {"status": "saved", "rev": rev}


@app.get("/api/model/backups")
async def get_model_backups(request: Request):
    """Admin: list saved model snapshots (for recovery)."""
    require_admin(request)
    return {"backups": persistence.list_model_backups()}


@app.get("/api/model/backup/{name}")
async def get_model_backup(name: str, request: Request):
    """Admin: fetch one backup's full content (for recovery/inspection)."""
    require_admin(request)
    data = persistence.read_model_backup(name)
    if data is None:
        raise HTTPException(404, "Backup not found")
    return data


@app.post("/api/model/restore/{name}")
async def restore_model_backup(name: str, request: Request):
    """Admin: restore a named backup as the current model."""
    require_admin(request)
    data = persistence.read_model_backup(name)
    if data is None:
        raise HTTPException(404, "Backup not found")
    data.pop("client_rev", None)
    rev = persistence.save_model(data)
    return {"status": "restored", "from": name, "rev": rev}




@app.get("/api/messages")
async def get_messages():
    messages = persistence.load_messages()
    # Sort newest first
    messages.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
    return messages



@app.post("/api/messages")
async def post_message(request: Request, message: dict, background_tasks: BackgroundTasks = None):
    if message.get("_hp_field"):
        raise HTTPException(status_code=400, detail="Rejected")
    text = (message.get("text") or "").strip()
    if not text or len(text) > 2000:
        raise HTTPException(status_code=400, detail="Invalid message")
    messages = persistence.load_messages()
    now = datetime.now(timezone.utc)
    new_message = {
        "id": str(now.timestamp()),
        "name": message.get("name", "Anonymous"),
        "text": message.get("text", ""),
        "parent_id": message.get("parent_id"),  # None for top-level messages
        "timestamp": now.isoformat()
    }
    messages.append(new_message)
    persistence.save_messages(messages)
    # Automatically trigger quick blue flash (0.25s instantaneous blue then instant restore to prev state)
    # on new blog message (top-level or reply). Uses same OFFICE_LAMP as poke, but blue color.
    # Behaves exactly like poke function (trans=0, 0.25s, restore logic) but with blue instead of off.
    # Uses BackgroundTasks pattern so POST returns instantly; poke button behavior is completely unchanged.
    if background_tasks is not None:
        background_tasks.add_task(home_assistant.perform_notify_blue)
    else:
        asyncio.create_task(home_assistant.perform_notify_blue())
    return new_message





@app.delete("/api/messages/{message_id}")
async def delete_message(request: Request, message_id: str):
    """Delete a message (and its replies). Admin session cookie required."""
    require_admin(request)

    messages = persistence.load_messages()
    original_len = len(messages)
    # Drop the message and any direct replies so the board doesn't leave orphans.
    messages = [
        m for m in messages
        if m.get("id") != message_id and m.get("parent_id") != message_id
    ]

    if len(messages) == original_len:
        raise HTTPException(status_code=404, detail="Message not found")

    persistence.save_messages(messages)
    return {"status": "deleted", "id": message_id}



@app.post("/api/blog/mark-read")
async def mark_blog_read():
    """Mark all current blog messages as read (for the smart notification hub on home). Personal use, no token needed."""
    persistence.save_last_blog_read()
    return {"status": "marked"}


# =============================================================================
# Simple placeholder routes for future sections (Golf, Clips, Blog)
# =============================================================================




@app.get("/api/golf/clubs")
async def get_clubs():
    return persistence.load_clubs()



@app.post("/api/golf/clubs")
async def update_clubs(request: Request, clubs: List[dict]):
    require_admin(request)
    persistence.save_clubs(clubs)
    return {"status": "saved"}


@app.get("/api/ha/orchestrate")
async def get_orchestrate():
    """Which orchestrated effect is running (if any)? Public read for honest UI state."""
    return home_assistant.fx_status()


@app.post("/api/ha/orchestrate")
async def post_orchestrate(request: Request, body: dict = Body(default={})):
    """Start/stop an orchestrated effect. Light-control gate (admin or guest access)."""
    require_light_control(request)
    if PUBLIC_MODE or not HA_ENABLED:
        raise HTTPException(403, "Home Assistant controls are disabled")
    action = (body.get("action") or "").strip()
    if action == "start":
        ids = [e for e in (body.get("entity_ids") or []) if isinstance(e, str) and e.startswith("light.")]
        if not ids:
            raise HTTPException(400, "entity_ids required")
        return await home_assistant.fx_start((body.get("pattern") or "").strip(), ids)
    if action == "stop":
        return await home_assistant.fx_stop()
    raise HTTPException(400, "action must be start or stop")


@app.post("/api/ha/vibe-log")
async def post_vibe_log(request: Request, body: dict = Body(default={})):
    """Log every 'Tell the house' command (understood or not) for later review."""
    text = str(body.get("text") or "")[:200].strip()
    if not text:
        raise HTTPException(400, "text required")
    persistence.log_vibe({
        "text": text,
        "understood": bool(body.get("understood")),
        "interpretation": str(body.get("interpretation") or "")[:200],
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    return {"status": "logged"}


@app.get("/api/ha/vibe-log")
async def get_vibe_log(request: Request):
    """Admin: review commands — failures first, so the parser can be taught."""
    require_admin(request)
    log = persistence.load_vibe_log()
    fails = [e for e in log if not e.get("understood")]
    return {"total": len(log), "failed": len(fails), "failures": fails[:100], "recent": log[:100]}


@app.get("/api/ha/light-history")
async def get_light_history(request: Request, limit: int = 300):
    """Admin: the ambient hourly light-usage snapshots, for learning usage patterns."""
    require_admin(request)
    log = persistence.load_light_history()
    return {"total": len(log), "snapshots": log[: max(1, min(limit, 1000))]}


@app.get("/api/os-state")
async def api_os_state():
    """The vitals Operating State (one prioritized truthful headline) for other pages —
    e.g. the lighting page's context-aware mode suggestions. Cached vitals, cheap."""
    try:
        ctx = await vitals.get_processed_vitals(use_cache=True)
        ins = ctx.get("insights") or {}
        return {
            "os_state": ins.get("os_state"),
            "readiness": ctx.get("readiness_score"),
            "sleep": ctx.get("sleep_score"),
        }
    except Exception:
        return {"os_state": None, "readiness": None, "sleep": None}


@app.get("/api/golf/rounds")
async def get_golf_rounds():
    """Rounds live server-side (volume) so they survive browser clears + sync across devices."""
    return persistence.load_golf_rounds()


@app.post("/api/golf/rounds")
async def update_golf_rounds(request: Request, rounds: List[dict]):
    require_admin(request)
    persistence.save_golf_rounds(rounds)
    return {"status": "saved"}




@app.get("/golf", response_class=HTMLResponse)
async def golf_page(request: Request):
    return _render("golf.html", {
        "request": request,
    })




@app.get("/clips", response_class=HTMLResponse)
async def clips_page(request: Request):
    return _render("clips.html", {
        "request": request,
        "has_admin_token": bool(ADMIN_TOKEN),
        "is_admin": is_admin_authenticated(request),
    })


# Real clips live in DATA_DIR/clips on the persistent volume (survive deploys).
# Upload from phone/desktop via admin; served at /media/clips/{name}.
_CLIP_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")
_CLIP_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_CLIP_EXTS = _CLIP_VIDEO_EXTS + _CLIP_IMAGE_EXTS
_CLIP_MAX_BYTES = 200 * 1024 * 1024  # 200 MB — comfortable for phone game clips


def _clips_dir() -> Path:
    d = persistence.DATA_PATH / "clips"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_clip_stem(name: str) -> str:
    """Filename-safe stem from a title or original filename."""
    import re
    base = Path(name).stem if name else "clip"
    base = re.sub(r"[^\w\s\-]+", "", base, flags=re.UNICODE).strip()
    base = re.sub(r"[\s_]+", "-", base).strip("-").lower()
    return (base[:80] or "clip")


def list_clips(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """All clips newest-first: small files on the volume + big videos hosted on
    R2 (referenced by URL). Both render identically; R2 items carry external=True
    and a null size_mb (the server doesn't hold the file, so it can't weigh it)."""
    items: List[tuple] = []  # (sort_ts, dict)

    d = _clips_dir()
    for p in d.iterdir():
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in _CLIP_EXTS:
            continue
        st = p.stat()
        kind = "image" if ext in _CLIP_IMAGE_EXTS else "video"
        items.append((st.st_mtime, {
            "name": p.name,
            "title": p.stem.replace("-", " ").replace("_", " ").title(),
            "size_mb": round(st.st_size / 1048576, 1),
            "date": datetime.fromtimestamp(st.st_mtime).strftime("%b %d, %Y"),
            "kind": kind,
            "url": f"/media/clips/{p.name}",
            "external": False,
        }))

    for c in persistence.load_external_clips():
        ts = c.get("added_ts") or 0
        items.append((ts, {
            "name": c.get("id"),
            "title": c.get("title") or "Video",
            "size_mb": None,
            "date": c.get("date") or (
                datetime.fromtimestamp(ts).strftime("%b %d, %Y") if ts else ""
            ),
            "kind": c.get("kind") or "video",
            "url": c.get("url"),
            "external": True,
        }))

    items.sort(key=lambda t: t[0], reverse=True)
    out = [d for _ts, d in items]
    return out[:limit] if limit is not None else out


@app.get("/api/clips")
async def api_clips():
    """List real clip files, newest first."""
    return {"clips": list_clips()}


@app.post("/api/clips/upload")
async def upload_clip(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
):
    """Admin-only upload. Saves into DATA_DIR/clips (same store the page already reads)."""
    require_admin(request)
    if not file or not file.filename:
        raise HTTPException(400, "No file provided")

    orig = Path(file.filename).name
    ext = Path(orig).suffix.lower()
    if ext not in _CLIP_EXTS:
        raise HTTPException(
            400,
            f"Unsupported type {ext or '(none)'}. Use mp4, mov, webm, m4v, jpg, png, webp, or gif.",
        )

    # Stream to disk with size guard (phones can send large clips).
    stem = _safe_clip_stem(title.strip() or orig)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_name = f"{stem}-{stamp}{ext}"
    dest = _clips_dir() / dest_name

    size = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _CLIP_MAX_BYTES:
                    out.close()
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise HTTPException(413, "File too large (max 200 MB)")
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(500, f"Upload failed: {e}") from e
    finally:
        await file.close()

    if size == 0:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(400, "Empty file")

    st = dest.stat()
    kind = "image" if ext in _CLIP_IMAGE_EXTS else "video"
    return {
        "status": "ok",
        "clip": {
            "name": dest.name,
            "title": dest.stem.replace("-", " ").replace("_", " ").title(),
            "size_mb": round(st.st_size / 1048576, 1),
            "date": datetime.fromtimestamp(st.st_mtime).strftime("%b %d, %Y"),
            "kind": kind,
            "url": f"/media/clips/{dest.name}",
        },
    }


@app.post("/api/clips/link")
async def add_clip_link(request: Request, body: dict = Body(default={})):
    """Admin-only: register a big video hosted on R2 (or any https URL) by link.
    The file is NOT uploaded here — the server just remembers the URL, so R2 keeps
    serving it with free egress and no credentials touch this box."""
    require_admin(request)
    url = (body.get("url") or "").strip()
    title = (body.get("title") or "").strip()
    kind = (body.get("kind") or "video").strip().lower()
    if not url.startswith("https://"):
        raise HTTPException(400, "URL must start with https://")
    if kind not in ("video", "image"):
        kind = "video"
    if not title:
        # Derive a title from the filename in the URL if none given.
        stem = Path(url.split("?")[0]).stem
        title = stem.replace("-", " ").replace("_", " ").title() or "Video"

    items = persistence.load_external_clips()
    entry = {
        "id": f"r2-{uuid.uuid4().hex[:12]}",
        "title": title,
        "url": url,
        "kind": kind,
        "date": datetime.now().strftime("%b %d, %Y"),
        "added_ts": time.time(),
    }
    items.insert(0, entry)
    persistence.save_external_clips(items)
    return {"status": "ok", "clip": {
        "name": entry["id"], "title": entry["title"], "url": entry["url"],
        "kind": entry["kind"], "date": entry["date"], "size_mb": None, "external": True,
    }}


@app.delete("/api/clips/{name}")
async def delete_clip(request: Request, name: str):
    """Admin-only: remove a clip — an R2 link (just forgets the URL) or a file on the volume."""
    require_admin(request)

    # R2/external link? Forget it (the R2 object itself is untouched — delete that
    # in the Cloudflare dashboard if you want the file gone too).
    ext = persistence.load_external_clips()
    remaining = [c for c in ext if c.get("id") != name]
    if len(remaining) != len(ext):
        persistence.save_external_clips(remaining)
        return {"status": "deleted", "name": name, "external": True}

    safe = Path(name).name
    if safe != name or ".." in name or "/" in name or "\\" in name:
        raise HTTPException(400, "Invalid name")
    p = _clips_dir() / safe
    if not p.is_file() or p.suffix.lower() not in _CLIP_EXTS:
        raise HTTPException(404, "Clip not found")
    try:
        p.unlink()
    except Exception as e:
        raise HTTPException(500, f"Could not delete: {e}") from e
    return {"status": "deleted", "name": safe}


@app.get("/media/clips/{name}")
async def media_clip(name: str):
    """Serve a clip file from the volume (path-traversal safe)."""
    safe = Path(name).name
    p = _clips_dir() / safe
    if not p.is_file() or p.suffix.lower() not in _CLIP_EXTS:
        raise HTTPException(404, "Clip not found")
    return FileResponse(p)




@app.get("/model", response_class=HTMLResponse)
async def model_page(request: Request):
    """Dedicated /model page - spatial home replica. Data served via /api/model.
    Editing is locked to admins; visitors get a read-only view."""
    return _render("model.html", {
        "request": request,
        "public_mode": PUBLIC_MODE,
        "site_name": SITE_NAME,
        "display_name": DISPLAY_NAME,
        "is_admin": is_admin_authenticated(request),
        "has_admin_token": bool(ADMIN_TOKEN),
    })




@app.get("/blog", response_class=HTMLResponse)
async def blog_page(request: Request):
    return _render("blog.html", {
        "request": request,
        "public_mode": PUBLIC_MODE,
        "site_name": SITE_NAME,
        "display_name": DISPLAY_NAME,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)


def main():
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)


if __name__ == "__main__":
    main()
