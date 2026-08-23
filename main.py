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
    GTM_CONTAINER_ID,
    HA_ENABLED,
    OURA_DAYS,
    OURA_TOKEN,
    PORT,
    PUBLIC_MODE,
    R2_PUBLIC_BASE,
    R2_UPLOAD_ENABLED,
    SITE_NAME,
    SITE_URL,
)
import gtm
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
            # Waking hours widened to match real life: 8am–1am. The old 10am–8pm
            # window missed the nighttime peak (when gaming/the game room is busiest),
            # which is exactly when the richest lighting patterns happen.
            if hour >= 8 or hour <= 1:
                snap = await home_assistant.capture_light_snapshot()
                if snap:
                    snap["ts"] = datetime.now(tz).isoformat()
                    persistence.log_light_snapshot(snap)
        except Exception as e:
            print(f"light history snapshot error: {e}")


async def _light_transition_loop():
    """Every ~2 min, poll the lights and record only what *changed* since last poll
    (on/off, color, effect, deliberate brightness step) with time-of-day + whatever
    you were doing on Xbox. Hourly snapshots capture *state*; this captures *intent*
    — the actual moves you make — so after a week we can see the scenes you set by
    hand (your real 'work mode') and propose automations that match them exactly."""
    tz = ZoneInfo("America/New_York")
    await asyncio.sleep(20)   # let the app settle
    while True:
        try:
            snap = await home_assistant.capture_light_snapshot()
            if snap and snap.get("lights"):
                prev = state.last_light_lights
                if prev is not None:
                    changes = home_assistant.diff_light_states(prev, snap["lights"])
                    if changes:
                        now = datetime.now(tz)
                        xb = state.last_xbox_data if isinstance(state.last_xbox_data, dict) else {}
                        game = xb.get("game") if xb.get("game") not in (None, "—") else ""
                        persistence.log_light_transitions({
                            "ts": now.isoformat(),
                            "weekday": now.weekday(),          # 0=Mon
                            "hour": now.hour,
                            "minute": now.minute,
                            "changes": changes,
                            "context": {
                                "xbox_game": game or "",
                                "playing": bool(xb.get("playing_now")),
                                "kind": xb.get("kind") or "",
                            },
                        })
                state.last_light_lights = snap["lights"]
        except Exception as e:
            print(f"light transition loop error: {e}")
        try:
            await asyncio.sleep(120)
        except asyncio.CancelledError:
            raise


async def _xbox_observer_loop():
    """Adaptive presence observer — spends the xbl.io 150/h budget where it matters.

    Cadence follows what you're doing:
      • playing a game → every 60s  (~60/h — near-real-time session lengths)
      • online, no game → every 3m  (~20/h — you're around, might launch something)
      • offline / away  → every 12m (~5/h — barely sips the budget)
      • API error       → back off 5m (don't hammer a down endpoint)
    A hard budget guard (API_BUDGET_PER_HOUR, shared with the page's status fetch)
    forces the slow cadence if we ever approach the ceiling, so 150/h is never hit."""
    INTERVALS = {"playing": 60, "online": 180, "offline": 720, "error": 300}
    await asyncio.sleep(10)   # let the app settle before the first sample
    while True:
        try:
            if xbox.api_calls_last_hour() >= xbox.API_BUDGET_PER_HOUR:
                delay = INTERVALS["offline"]                       # budget guard
            else:
                sample = await xbox.poll_presence_sample()
                if sample is None:
                    delay = INTERVALS["error"]                     # API down — log nothing
                else:
                    game, device, pstate = sample
                    xbox.record_presence_sample(game, device)
                    # Permanently stamp closed sessions with Oura HR for their
                    # window (rate-limited internally; Oura call is cached).
                    await xbox.sweep_session_hr()
                    if game:
                        delay = INTERVALS["playing"]
                    elif str(pstate).lower() == "online":
                        delay = INTERVALS["online"]
                    else:
                        delay = INTERVALS["offline"]
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"xbox observer error: {e}")
            delay = INTERVALS["error"]
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise


async def _vitals_history_loop():
    """Every ~3 hours, distill the day's vitals into one compact snapshot and
    upsert it into our longitudinal history (idempotent per calendar day). Uses
    the cached vitals so it never burns Oura quota, and only logs days with real
    data. This is what lets us mine long-term trends beyond Oura's window."""
    if not OURA_TOKEN:
        return
    await asyncio.sleep(30)   # let the app settle
    while True:
        try:
            ctx = await vitals.get_processed_vitals(days=OURA_DAYS, use_cache=True)
            vitals.record_daily_snapshot(ctx)
        except Exception as e:
            print(f"vitals history loop error: {e}")
        try:
            await asyncio.sleep(3 * 3600)
        except asyncio.CancelledError:
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    if OURA_TOKEN:
        state.oura_client = OuraClient(OURA_TOKEN)
    elif PUBLIC_MODE:
        raise RuntimeError("PUBLIC_MODE requires OURA_TOKEN")
    history_task = asyncio.create_task(_light_history_loop())
    transition_task = asyncio.create_task(_light_transition_loop())
    xbox_task = asyncio.create_task(_xbox_observer_loop())
    vitals_task = asyncio.create_task(_vitals_history_loop())
    yield
    history_task.cancel()
    transition_task.cancel()
    xbox_task.cancel()
    vitals_task.cancel()
    if state.oura_client:
        await state.oura_client.close()
        state.oura_client = None


app = FastAPI(title="HENDERBURGH", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(RateLimitMiddleware)
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


def _render(template_name: str, context: Dict[str, Any]) -> HTMLResponse:
    template = templates.env.get_template(template_name)
    html = template.render(**context)
    return HTMLResponse(gtm.inject(html, GTM_CONTAINER_ID))


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


@app.post("/api/privacy/reveal")
async def toggle_reveal(request: Request, body: dict = Body(default={})):
    """Admin-only: flip the work-hours 'reveal my full day' cookie. Coworkers are
    never admin, so this can only ever reveal the owner's own view."""
    require_admin(request)
    on = bool(body.get("on"))
    resp = JSONResponse({"status": "ok", "revealed": on})
    if on:
        resp.set_cookie("reveal_private", "1", max_age=12 * 3600, httponly=False, samesite="lax")
    else:
        resp.delete_cookie("reveal_private")
    return resp


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

@app.get("/oldhomepage", response_class=HTMLResponse)
async def old_homepage(request: Request):
    """The original home page — kept live at a stable URL for reference after
    the day-timeline redesign (home-concept -> /) replaced it at "/". Not
    linked from nav; reachable only by direct URL."""
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

    # Bare phrase only — the template supplies the "Updated" label. Emitting
    # "updated ..." here too produced the visible "Updated updated 40m ago".
    hr_updated_ago = "recently"
    if latest_hr_timestamp and hr_age_minutes is not None:
        try:
            ts = datetime.fromisoformat(latest_hr_timestamp.replace("Z", "+00:00"))
            local_tz = ZoneInfo("America/New_York")
            local_time = ts.astimezone(local_tz)

            if hr_age_minutes < 60:
                hr_updated_ago = f"{hr_age_minutes}m ago"
            else:
                hr_updated_ago = local_time.strftime("at %-I:%M %p").replace(" 0", " ")
        except Exception:
            hr_updated_ago = "recently"

    hr_ctx = {
        "bpm": latest_hr,
        "updated_ago": hr_updated_ago,
        "age_minutes": hr_age_minutes,
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

    # Yesterday, replayed: the cross-domain timeline (heart + games + sleep +
    # house lights + spoken commands) promoted to the OS level, with the
    # morning narration. This is the site's thesis in one artifact.
    _reveal = is_admin_authenticated(request) and request.cookies.get("reveal_private") == "1"
    try:
        _lib = await xbox.get_title_history()
        _ach = await xbox.get_recent_achievements(_lib)
        replay = await xbox.compute_recent_replay(_ach, reveal=_reveal)
    except Exception as e:
        print(f"home replay error: {e}")
        replay = {"has_data": False}
    if replay.get("narration"):
        from html import escape as _esc
        parts = _esc(replay["narration"]).split("**")
        for i in range(1, len(parts), 2):
            parts[i] = f'<strong class="text-white font-semibold">{parts[i]}</strong>'
        replay["narration_html"] = "".join(parts)

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
        "replay": replay,
        "time_greeting": time_greeting_now(),
    })


def _day_relative_label(day_iso: str) -> str:
    """"Today" / "Yesterday" / "N days ago" for a logical day. Mirrors the JS
    label in home-concept.html so the server's first paint and the explorer's
    later paints read identically."""
    try:
        d = date.fromisoformat(day_iso)
    except Exception:
        return ""
    n = (xbox._logical_today() - d).days
    if n <= 0:
        return "Today"
    if n == 1:
        return "Yesterday"
    return f"{n} days ago"


def build_day_timeline(replay: dict) -> list:
    """Turn ONE day's replay payload into the ordered list of real events the
    home page's vertical timeline renders (wake / work markers / sessions /
    peak / latest reading).

    Lifted out of home() unchanged in behaviour so the multi-day explorer's
    /api/day-replay can hand the browser the exact same event list the server
    rendered for the initial day — one builder, so a day fetched by JS can
    never disagree with a day rendered by Jinja.

    Privacy: every session here comes off `replay["sessions"]`, which
    compute_day_replay already filtered through in_work_hours()/reveal. This
    function adds no new data source, so it cannot leak anything.
    """
    timeline = []
    if not replay or not replay.get("has_data"):
        return timeline

    _now_et = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    # The timeline axis is minutes since midnight of the LOGICAL day, which
    # runs 4 AM → 4 AM (see xbox._DAY_START_HOUR). Before 4 AM we are still in
    # yesterday's story, so "now" is 1440+ on that day's axis.
    if _now_et.hour < xbox._DAY_START_HOUR:
        _now_min = 1440 + _now_et.hour * 60 + _now_et.minute
        _logical_today = (_now_et - timedelta(days=1)).date()
    else:
        _now_min = _now_et.hour * 60 + _now_et.minute
        _logical_today = _now_et.date()

    try:
        _day = date.fromisoformat(replay.get("day_iso") or "")
    except Exception:
        _day = _logical_today
    _is_today = (_day == _logical_today)
    _weekday = _day.weekday() < 5

    # Last night's sleep opens the day (sleep → wake → work). Owner-only:
    # compute_day_replay sets replay["sleep"] to None in the public view.
    if replay.get("sleep"):
        sl = replay["sleep"]
        timeline.append({"x": sl["x"], "clk": sl["start_clk"], "kind": "sleep",
                         "title": "Asleep",
                         "detail": f"{sl['start_clk']} – {sl['wake_clk']} · {sl['dur']} asleep."})

    if replay.get("wake"):
        w = replay["wake"]
        timeline.append({"x": w["x"], "clk": w["clk"], "kind": "wake",
                         "title": "Woke up", "detail": "The ring's first reading after sleep — the day begins."})

    # Work markers: only on a weekday, and for TODAY only once we're actually
    # past each threshold. A finished past weekday gets both. Symmetric pair
    # matching the exact WORKING/RELAXING boundary in time_greeting_now()
    # (8:30 AM / 5 PM) — the same cascade the hero headline uses.
    if _weekday and (not _is_today or _now_min >= 510):
        timeline.append({"x": 510.0, "clk": "8:30 AM", "kind": "work",
                         "title": "Started work", "detail": "The workday begins — status turns to Working."})
    if _weekday and (not _is_today or _now_min >= 1020):
        timeline.append({"x": 1020.0, "clk": "5:00 PM", "kind": "work-end",
                         "title": "Finished work", "detail": "The workday ends — status turns to Relaxing."})

    for s in replay.get("sessions", []):
        verb = {"game": "Played", "media": "Watched", "workout": "Worked out —"}.get(s["kind"], "")
        dur = "<1 min" if s["dur_min"] < 1 else f"{int(s['dur_min'])} min"
        detail = f"{verb} {s['name']} · {dur}"
        if s.get("hr"):
            detail += f" · heart averaged {s['hr']} bpm"
        timeline.append({"x": s["x0"], "clk": s["clk"], "kind": s["kind"],
                         "title": s["name"], "detail": detail})

    if replay.get("peak"):
        p = replay["peak"]
        above = (f" — {p['bpm'] - replay['low']['bpm']} above the calmest point"
                 if replay.get("low") else "")
        timeline.append({"x": p["x"], "clk": p["clk"], "kind": "peak",
                         "title": "Heart rate peaked", "detail": f"{p['bpm']} bpm{above}."})

    if replay.get("current"):
        c = replay["current"]
        label = "Latest reading" if _is_today else "Last reading"
        timeline.append({"x": c["x"], "clk": c["clk"], "kind": "now",
                         "title": label, "detail": f"{c['bpm']} bpm — the most recent the ring recorded."})

    timeline.sort(key=lambda e: e["x"])
    # De-dupe events landing on the same minute+kind (e.g. peak == current).
    seen = set()
    deduped = []
    for e in timeline:
        key = (round(e["x"]), e["kind"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """The day-timeline home redesign — promoted from /home-concept to the
    live front door. Ported the aurora (real house-light colours), the
    pointer-tracked card spotlight, and the ring-freshness HUD dot from the
    original home page (still live at /oldhomepage) so the new design keeps
    that page's signature "the site mirrors the house" feel."""
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
    steps_ctx = {
        "count": metrics.get("steps"),
        "miles": round(metrics["steps"] * 0.0005, 1) if metrics.get("steps") is not None else None,
    }
    hr_ctx = {"bpm": metrics.get("latest_hr"), "age_minutes": metrics.get("hr_age_minutes")}

    _reveal = is_admin_authenticated(request) and request.cookies.get("reveal_private") == "1"
    try:
        _lib = await xbox.get_title_history()
        _ach = await xbox.get_recent_achievements(_lib)
        # Open on YESTERDAY: it's a COMPLETE day, so the timeline shows its
        # full form (wake → work → sessions → last reading) instead of today's
        # half-drawn version. Today is one step to the right in the explorer.
        # If yesterday genuinely has no ring data, fall back to the old
        # never-blank walk-back so the page still shows a real day.
        replay = await xbox.compute_day_replay(_ach, day_offset=1, reveal=_reveal)
        if not replay.get("has_data"):
            replay = await xbox.compute_recent_replay(_ach, reveal=_reveal)
    except Exception as e:
        print(f"home-concept replay error: {e}")
        replay = {"has_data": False}

    # Build the day's timeline as an ordered list of REAL events only — no
    # dead space. Shared with /api/day-replay so a day fetched by the
    # explorer's JS is built by the exact same code as this first paint.
    timeline = build_day_timeline(replay)

    # Unread count for the office-lamp poke badge (same source as the old
    # home page's notifier — see old_homepage() above).
    blog_unread_count = 0
    latest_post = None
    try:
        all_messages = persistence.load_messages()
        last_read = persistence.load_last_blog_read()
        blog_unread_count = len([m for m in all_messages if str(m.get("timestamp", "")) > last_read])

        # Latest top-level post + its reply count, for the Blog system tile —
        # "get the overall goal of everything from one glance" means showing
        # actual content, not just a link.
        top_level = [m for m in all_messages if not m.get("parent_id")]
        if top_level:
            top_level.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
            latest_post = dict(top_level[0])
            latest_post["reply_count"] = len(
                [m for m in all_messages if m.get("parent_id") == latest_post.get("id")]
            )
    except Exception:
        pass

    # Latest golf round for the Golf system tile.
    latest_round = None
    try:
        rounds = persistence.load_golf_rounds()
        if rounds:
            rounds_sorted = sorted(rounds, key=lambda r: r.get("date", ""), reverse=True)
            latest_round = rounds_sorted[0]
    except Exception:
        latest_round = None

    # Room count for the Model system tile. A live 3D embed on the home page
    # would repeat the exact three.js weight problem already flagged for
    # /model (the single heaviest asset on the site) — on a page opened many
    # times a day, a real stat is the honest, cheap version of "a small
    # model of what I built."
    model_rooms = 0
    try:
        model_data = persistence.load_model()
        model_rooms = len((model_data or {}).get("rooms") or {})
    except Exception:
        model_rooms = 0

    recent_clips = list_clips(limit=3)

    return _render("home-concept.html", {
        "request": request,
        "public_mode": PUBLIC_MODE,
        "display_name": DISPLAY_NAME,
        "steps": steps_ctx,
        "heart_rate": hr_ctx,
        "replay": replay,
        "blog_unread_count": blog_unread_count,
        "latest_post": latest_post,
        "latest_round": latest_round,
        "model_rooms": model_rooms,
        "recent_clips": recent_clips,
        "timeline": timeline,
        # Day-explorer bounds. `day_iso` is the day this first paint shows
        # (normally yesterday); `today_iso` is the newest day the explorer may
        # step to — the LOGICAL today, so before 4 AM we don't offer a day that
        # hasn't started yet.
        "day_iso": replay.get("day_iso") or "",
        "today_iso": xbox._logical_today().isoformat(),
        "day_rel": _day_relative_label(replay.get("day_iso") or ""),
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

    # Work-hours privacy: personal activity during weekday work hours is hidden
    # from the public view (coworkers). Only the admin, with reveal toggled on
    # (cookie), sees their own full day. Computed here (needs only `request`) so
    # the 'Recently Launched' fallback below can also honor it.
    reveal_private = is_admin_authenticated(request) and request.cookies.get("reveal_private") == "1"

    # Recently-played, cleaned to REAL games only (drop Home / Xbox App / dashboard
    # noise) and prettified names — this feeds both the table and the insights.
    formatted_log = []
    for entry in raw_log:
        name = entry.get("game", "—")
        if not xbox.is_real_game(name):
            continue
        ts = entry.get("timestamp", "")
        played = ts
        entry_dt = None
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            # naive-ET moment for the work-hours check (stored ts is naive ET)
            entry_dt = dt.replace(tzinfo=None)
            played = dt.strftime("%b %-d, %Y • %-I:%M %p")
        except Exception:
            pass
        # Skip work-hours launches from the public fallback table.
        if not reveal_private and xbox.in_work_hours(entry_dt):
            continue
        formatted_log.append({
            "game": xbox._clean_title(name),
            "timestamp": ts,
            "played": played,
            "device": entry.get("device", "") or "",
        })

    xbox_display = dict(xbox_data) if xbox_data else {}

    if not reveal_private:
        xbox_display = xbox.redact_status_for_public(xbox_display)

    # Truthful profile stats only — surface a number when the API actually returned
    # one, otherwise the template simply omits it (no fake zeros, no stale "Gold").
    def _real(v):
        try:
            return int(v) > 0
        except Exception:
            return bool(v)
    xbox_display["has_gamerscore"] = _real(xbox_display.get("gamerscore"))
    if xbox_display["has_gamerscore"]:
        xbox_display["gamerscore_display"] = f"{int(xbox_display['gamerscore']):,}"
    xbox_display["has_tenure"] = _real(xbox_display.get("tenure"))

    # Gaming intelligence + signature "cover art" for the now-playing hero.
    # True sessions (server-side observer) power hours/streaks; the legacy
    # change log only contributes launch counts.
    true_sessions = persistence.load_xbox_sessions()
    insights = xbox.compute_gaming_insights(raw_log, xbox_display.get("game", ""), true_sessions)

    # The Recently-Played timeline is now real play sessions (start / length /
    # device) when the observer has any — otherwise fall back to the legacy
    # launch log so the section is never empty during the transition.
    session_log = xbox.sessions_for_display(true_sessions, reveal=reveal_private)

    # Render **bold** in insight lines to safe HTML (escape first, then emphasize).
    from html import escape as _esc
    def _md_bold(s: str) -> str:
        parts = _esc(s).split("**")
        for i in range(1, len(parts), 2):
            parts[i] = f'<strong class="text-white font-semibold">{parts[i]}</strong>'
        return "".join(parts)
    insights["insights_html"] = [_md_bold(x) for x in insights.get("insights", [])]

    # Real games library (cover art + achievements + last-played) from Xbox.
    library = await xbox.get_title_history()
    games = xbox.recent_games_display(library, limit=12)

    # Log today's gamerscore + per-game progress, then compute momentum from the
    # accumulated history (earned this week, which games are climbing).
    xbox.record_xbox_snapshot(xbox_display.get("gamerscore") or 0, library)
    xbox_hist = persistence.load_xbox_history()
    progress = xbox.compute_progress(xbox_hist, library, xbox_display.get("gamerscore") or 0)

    # The Story: cross-referenced personal narration from the logs we keep.
    story = {
        "body": xbox.compute_body_controller(true_sessions, persistence.load_vitals_history()),
        "eras": xbox.compute_eras(true_sessions, raw_log, library),
        "week": xbox.compute_week_review(true_sessions, xbox_hist),
    }
    story["body"] = story["body"] + xbox.compute_pulse_story(true_sessions)
    story["body_html"] = [_md_bold(x) for x in story["body"]]
    story["eras_html"] = [_md_bold(x) for x in story["eras"]]
    story["week_html"] = _md_bold(story["week"]["text"]) if story["week"].get("text") else ""

    # The Pulse chart: daily play hours + session heart rate on one timeline.
    pulse = xbox.compute_pulse_chart(true_sessions, xbox_hist)

    # Achievements: real in-game moments (unlock times + gamerscore).
    achievements = await xbox.get_recent_achievements(library)
    unlocks = xbox.recent_unlocks_display(achievements, limit=8, reveal=reveal_private)

    # The Replay: yesterday's full HR curve annotated with sessions, sleep,
    # workouts, and achievement unlocks pinned to the exact minute.
    try:
        replay = await xbox.compute_recent_replay(achievements, reveal=reveal_private)
    except Exception as e:
        print(f"day replay error: {e}")
        replay = {"has_data": False}
    if replay.get("narration"):
        replay["narration_html"] = _md_bold(replay["narration"])

    hero_game = xbox_display.get("game", "")
    if not xbox.is_real_game(hero_game):
        hero_game = insights.get("stats", {}).get("favorite", "")
    # Live only — the backend's playing_now is derived from actual device
    # activity, never from lastSeen history.
    playing_now = bool(xbox_display.get("playing_now"))
    # The currently-playing game is, by definition, the most recently played
    # library entry — so use its cover directly when live (reliable, no name
    # matching). Otherwise fuzzy-match the favorite to its box art.
    if playing_now and library:
        hero_cover = library[0].get("image", "") or xbox.find_cover(library, hero_game)
    else:
        hero_cover = xbox.find_cover(library, hero_game)
    hero = {
        "game": xbox._clean_title(hero_game) if hero_game else "",
        "signature": xbox.game_signature(hero_game or "xbox"),
        "playing_now": playing_now,
        "cover": hero_cover,
        "kind": xbox.classify_title(xbox_display.get("game", "")),
    }

    return _render("xbox.html", {
        "request": request,
        "xbox": xbox_display,
        "log": formatted_log,
        "session_log": session_log,
        "insights": insights,
        "hero": hero,
        "games": games,
        "progress": progress,
        "story": story,
        "pulse": pulse,
        "replay": replay,
        "unlocks": unlocks,
        "is_admin": is_admin_authenticated(request),
        "reveal_private": reveal_private,
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
        vitals.record_daily_snapshot(ctx)   # log today's vitals into the longitudinal history

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

        source = "live" if latest_hr is not None else "none"

        if latest_hr is None:
            # Fallback
            recent_start = (now - timedelta(days=1)).date().isoformat()
            detailed = await state.oura_client.get_sleep(recent_start, now.date().isoformat())
            if detailed:
                latest_detailed = detailed[-1] if detailed else None
                latest_hr = _safe_get(latest_detailed, "average_heart_rate") or _safe_get(latest_detailed, "lowest_heart_rate")
                if latest_hr is not None:
                    source = "resting"

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
            "age_minutes": age_minutes,
            "source": source
        })
    except Exception:
        return JSONResponse({"bpm": None, "timestamp": None, "source": "none"})




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
async def api_xbox_status(request: Request):
    """Live status. During weekday work hours the public copy is redacted (no
    current game / device / fresh last-seen) — this feeds the home page's Live
    Now, so 'gaming at 2pm' can never surface to coworkers."""
    d = await fetch_xbox_status()
    reveal = is_admin_authenticated(request) and request.cookies.get("reveal_private") == "1"
    return d if reveal else xbox.redact_status_for_public(d)


@app.get("/api/day-replay")
async def api_day_replay(request: Request, day: str = ""):
    """Day Explorer: the full replay payload for ANY past day.

    Same privacy contract as the xbox page — work-hours gaming/watching/unlocks
    and ALL sleep stay hidden unless the admin has explicitly revealed."""
    try:
        d = date.fromisoformat(day)
    except Exception:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    if d > xbox._now_et().date():
        raise HTTPException(status_code=400, detail="day is in the future")

    reveal = is_admin_authenticated(request) and request.cookies.get("reveal_private") == "1"
    try:
        library = await xbox.get_title_history()
        # Achievement unlock times only reach ~a few days back; older days simply
        # come back with no markers rather than failing the whole request.
        achievements = await xbox.get_recent_achievements(library)
    except Exception as e:
        print(f"day replay achievements error: {e}")
        achievements = []
    try:
        payload = await xbox.compute_day_replay(achievements, reveal=reveal, day=d)
    except Exception as e:
        # A blank/odd day must degrade to "nothing here", never a 500.
        print(f"day replay error for {day}: {e}")
        payload = {"has_data": False}
    payload["day_iso"] = d.isoformat()
    # The home page's day explorer renders the same vertical timeline for every
    # day, so it needs the same event list the server builds for the first
    # paint. Built here (not in JS) so both paths share one implementation and
    # one privacy filter.
    payload["timeline"] = build_day_timeline(payload)
    return payload


@app.get("/api/day-index")
async def api_day_index(request: Request, days: int = 30):
    """Which recent days are worth opening — powers the Day Explorer picker.
    Cheap and cached; see xbox.compute_day_index for why it isn't N replays."""
    reveal = is_admin_authenticated(request) and request.cookies.get("reveal_private") == "1"
    try:
        return {"days": await xbox.compute_day_index(days=days, reveal=reveal)}
    except Exception as e:
        print(f"day index error: {e}")
        return {"days": []}


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


@app.delete("/api/ha/access-requests/{req_id}")
async def delete_access_request(req_id: str, request: Request):
    """Protected: dismiss a single access request. Same require_admin() session-
    cookie auth as the GET above (this dropped during the services/ refactor --
    home-assistant.html's dismiss button was still calling it and 404ing)."""
    require_admin(request)
    reqs = persistence.load_access_requests()
    reqs = [r for r in reqs if r.get("id") != req_id]
    persistence.save_access_requests(reqs)
    return {"status": "deleted"}




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
    name = str(message.get("name") or "").strip()[:80] or "Anonymous"
    messages = persistence.load_messages()
    now = datetime.now(timezone.utc)
    new_message = {
        "id": str(now.timestamp()),
        "name": name,
        "text": text,
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


@app.get("/api/ha/light-transitions")
async def get_light_transitions_raw(request: Request, limit: int = 2000):
    """Admin: the raw deliberate-change log (every logged transition, with exact
    timestamp + which lights changed), for pattern analysis finer-grained than
    the hour-bucketed candidate_automations in /api/ha/insights — e.g. finding
    multi-light bursts (several lights changed within the same minute or two)."""
    require_admin(request)
    log = persistence.load_light_transitions()
    return {"total": len(log), "transitions": log[: max(1, min(limit, 5000))]}


@app.get("/api/ha/insights")
async def get_light_insights(request: Request):
    """Admin: the full lighting-intelligence report derived from snapshots +
    transitions + commands — coverage, per-light profiles, what you set lights to,
    and candidate automations. This is the data we mine to design real automations."""
    require_admin(request)
    from services import light_insights
    return light_insights.compute_insights()


@app.get("/insights")
async def insights_page(request: Request):
    """Private House Insights panel. The page shell is harmless; the actual data
    is fetched from the admin-gated /api/ha/insights, so it only fills in for you."""
    return _render("insights.html", {
        "request": request,
        "is_admin": is_admin_authenticated(request),
        "display_name": DISPLAY_NAME,
        "site_name": SITE_NAME,
    })


@app.get("/api/xbox/history")
async def get_xbox_history(request: Request, limit: int = 400):
    """Admin: the daily Xbox gamerscore/progress log, for reviewing momentum."""
    require_admin(request)
    hist = persistence.load_xbox_history()
    # Trim the heavy per-game 'scores' map from the listing for readability.
    slim = [{k: v for k, v in h.items() if k != "scores"} for h in hist]
    return {"total": len(hist), "days": slim[-max(1, min(limit, 800)):]}


@app.get("/api/vitals/history")
async def get_vitals_history(request: Request, limit: int = 400):
    """Admin: the longitudinal daily vitals log — one snapshot per day, so trends
    can be mined beyond Oura's rolling window."""
    require_admin(request)
    hist = persistence.load_vitals_history()
    return {"total": len(hist), "days": hist[-max(1, min(limit, 800)):]}


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


@app.get("/arcade", response_class=HTMLResponse)
async def arcade_page(request: Request):
    """Physical 64x64 LED-matrix arcade. This page launches the local control
    panel served by wled-m1-arcade/arcade_server.py on the home network.
    Set ARCADE_URL to the Mac's LAN address (reserve its DHCP IP for stability)."""
    return _render("arcade.html", {
        "request": request,
        "arcade_url": os.getenv("ARCADE_URL", "http://192.168.40.203:7333"),
    })




@app.get("/clips", response_class=HTMLResponse)
async def clips_page(request: Request):
    return _render("clips.html", {
        "request": request,
        "has_admin_token": bool(ADMIN_TOKEN),
        "is_admin": is_admin_authenticated(request),
        "r2_enabled": R2_UPLOAD_ENABLED,
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
    stamp = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d-%H%M%S")
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


@app.post("/api/clips/presign")
async def presign_clip_upload(request: Request, body: dict = Body(default={})):
    """Admin-only: hand the phone a short-lived presigned PUT URL so it uploads the
    clip DIRECTLY to R2, sidestepping Cloudflare's 100 MB proxy cap that blocks big
    clips through the app. The client then registers the public URL via /link."""
    require_admin(request)
    from clients import r2
    if not r2.enabled():
        raise HTTPException(503, "Direct upload not configured (missing R2 credentials).")

    filename = (body.get("filename") or "").strip()
    title = (body.get("title") or "").strip()
    ext = Path(filename).suffix.lower()
    if ext not in _CLIP_EXTS:
        raise HTTPException(400, f"Unsupported type {ext or '(none)'}.")

    stem = _safe_clip_stem(title or Path(filename).name)
    stamp = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d-%H%M%S")
    key = f"clips/{stem}-{stamp}{ext}"
    ct = r2.content_type_for(ext)
    put_url = r2.presign_put(key, ct, expires=3600)
    public_url = f"{R2_PUBLIC_BASE}/{key}"
    kind = "image" if ext in _CLIP_IMAGE_EXTS else "video"
    return {
        "put_url": put_url,
        "public_url": public_url,
        "key": key,
        "content_type": ct,
        "kind": kind,
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
    # Smart shorthand: a bare filename ("wedding.mp4") auto-expands to the R2 base.
    if url and not url.lower().startswith("http"):
        url = f"{R2_PUBLIC_BASE}/{url.lstrip('/')}"
    if not url.startswith("https://"):
        raise HTTPException(400, "Enter a filename (e.g. wedding.mp4) or a full https:// link")
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
        "date": datetime.now(ZoneInfo("America/New_York")).strftime("%b %d, %Y"),
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
    gone = next((c for c in ext if c.get("id") == name), None)
    remaining = [c for c in ext if c.get("id") != name]
    if len(remaining) != len(ext):
        persistence.save_external_clips(remaining)
        # If this link points at our own R2 bucket and we uploaded it (have creds),
        # delete the underlying object too so we don't leave orphans behind.
        url = (gone or {}).get("url") or ""
        if R2_UPLOAD_ENABLED and url.startswith(R2_PUBLIC_BASE + "/"):
            try:
                import httpx
                from clients import r2
                key = url[len(R2_PUBLIC_BASE) + 1:].split("?")[0]
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.delete(r2.presign_delete(key))
            except Exception as e:
                print(f"R2 object delete failed (link forgotten anyway): {e}")
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


# ------------------------------------------------------------------
# NICOLE'S DATE INVITATION (personal, one-off; see services/dateplan.py).
# Placed at the very end of the routes for a specific reason: this file
# has a lifespan() decorator up at line ~182, and editing anywhere near
# it once broke deploys by stealing the decorator. Everything below sits
# 2000 lines away from that trap.
# ------------------------------------------------------------------
from services import dateplan as _dateplan  # noqa: E402  (bottom-of-file on purpose)


@app.get("/date/{secret}", response_class=HTMLResponse)
async def date_page(secret: str):
    """Behind a word only she has (obscurity, not auth -- stakes are
    embarrassment, not compromise). Any other /date/* 404s rather than
    hinting the real one exists."""
    if secret != _dateplan.SECRET:
        raise HTTPException(status_code=404)
    path = Path(__file__).parent / "templates" / "date.html"
    return HTMLResponse(path.read_bytes())


@app.get("/api/date/inbox")
async def api_date_inbox(token: str = ""):
    """Read-only view of every pick logged so far. Token-gated (ADMIN_TOKEN
    from env) so only he can read the log. Same log file the poster writes."""
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing token")
    return {"count": len(_dateplan.picks(limit=500)),
            "picks": _dateplan.picks(limit=500)}


@app.post("/api/date/pick")
async def api_date_pick(request: Request, background_tasks: BackgroundTasks):
    """She (or a friend, with ?v=<name>) tapped a card. Append the pick to
    the jsonl and poke the office lamp so he sees it wherever he is in
    the room -- same signal path as the manual poke button on the home
    hub, just fired automatically instead of by a click."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    row = _dateplan.record(
        movie=body.get("movie"),
        dinner=body.get("dinner"),
        morning=body.get("morning"),
        heli=bool(body.get("heli")),
        note=body.get("note"),
        viewer=body.get("viewer"),
    )
    # Fire-and-forget lamp blink so the POST returns instantly. Same
    # BackgroundTasks pattern /api/ha/poke uses.
    background_tasks.add_task(home_assistant.perform_poke_blink)
    return {"ok": True, "row": row}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)


def main():
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)


if __name__ == "__main__":
    main()
