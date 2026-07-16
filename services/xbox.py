"""Xbox Live status via xbl.io."""

from __future__ import annotations

import hashlib
import time
from collections import Counter, deque
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from config import (
    XBL_API_KEY,
    XBL_GAMERTAG,
    XBL_PLACEHOLDER_GAMERTAGS,
    XBL_PLACEHOLDER_KEYS,
    XBL_XUID,
    XBOX_CACHE_TTL_SECONDS,
)
from services import persistence, state

# ── API budget accounting ────────────────────────────────────────────────────
# xbl.io free tier = 150 requests/hour. EVERY real call to xbl.io (the observer's
# presence poll AND the page's status fetch) is stamped here so the adaptive
# scheduler can spend the budget aggressively while playing yet never exceed it.
API_BUDGET_PER_HOUR = 140          # hard ceiling; leaves headroom under 150
_api_call_times: deque = deque(maxlen=300)


def note_api_call():
    _api_call_times.append(time.time())


def api_calls_last_hour() -> int:
    cutoff = time.time() - 3600
    while _api_call_times and _api_call_times[0] < cutoff:
        _api_call_times.popleft()
    return len(_api_call_times)


async def fetch_xbox_status():
    

    # If using placeholder values, return not_configured immediately (no API call)
    # Detection: ONLY trigger on the exact example placeholder strings from .env.example.
    # Real values (including the user's actual key and gamertag="NutNutBiinks") will proceed to call xbl.io.
    # See comment above the XBL_ assignments for full explanation of detection logic.
    if not XBL_API_KEY or XBL_API_KEY in XBL_PLACEHOLDER_KEYS or not XBL_GAMERTAG or XBL_GAMERTAG in XBL_PLACEHOLDER_GAMERTAGS:
        return {"status": "not_configured", "state": "Unknown", "game": "—"}

    # Simple time-based cache: serve last known good data instantly if still fresh.
    # This is the main protection against burning the 150 req/h free tier.
    now = time.time()
    if state.last_xbox_data.get("status") == "ok":
        age = now - state._last_xbox_fetch_time
        if age < XBOX_CACHE_TTL_SECONDS:
            return state.last_xbox_data

    headers = {
        "X-Authorization": XBL_API_KEY,
        "Accept": "application/json"
    }

    async with httpx.AsyncClient() as client:
        try:
            xuid = XBL_XUID
            if not xuid:
                # Step 1: Resolve gamertag to XUID (only if no direct XUID provided)
                profile_url = f"https://xbl.io/api/v2/player/gamertag/{XBL_GAMERTAG}"
                profile_res = await client.get(profile_url, headers=headers, timeout=10)

                if profile_res.status_code != 200:
                    key_preview = XBL_API_KEY[:8] + "..." if XBL_API_KEY else "None"
                    print(f"Xbox API error (profile): status={profile_res.status_code} body={profile_res.text[:300]} key_preview={key_preview} gamertag={XBL_GAMERTAG}")
                    if profile_res.status_code == 401:
                        print("Xbox: Invalid API key detected - verify your XBL_API_KEY from xbl.io app console (it should return 200 on /account test).")
                    return state.last_xbox_data

                profile = profile_res.json()
                xuid = profile.get("xuid")

                if not xuid:
                    print("Xbox API error: no xuid returned in profile response")
                    return state.last_xbox_data

            # Step 2: Get presence using XUID (either from resolution or direct XBL_XUID)
            presence_url = f"https://xbl.io/api/v2/{xuid}/presence"
            note_api_call()
            presence_res = await client.get(presence_url, headers=headers, timeout=10)

            if presence_res.status_code != 200:
                key_preview = XBL_API_KEY[:8] + "..." if XBL_API_KEY else "None"
                print(f"Xbox API error (presence): status={presence_res.status_code} body={presence_res.text[:300]} key_preview={key_preview} xuid={xuid}")
                return state.last_xbox_data

            presence = presence_res.json() or {}

            # The xbl.io /presence (and /account) responses wrap the actual data under "content"
            data = presence.get("content") or presence

            # NOTE: the top-level `state` is Xbox's SOCIAL/messaging presence, which
            # privacy settings routinely report as "Offline" even while you're
            # mid-game. We override it below using real device activity.
            raw_state = data.get("state", "Unknown")

            # Step 3: TWO separate truths, never conflated:
            #   game      = what's on a device RIGHT NOW (live devices[].titles only)
            #   last_seen = what you played LAST and when (historical — from lastSeen)
            # The old code fell back to lastSeen for `game`, which made a console
            # that's been off all day look Online and 'playing' its last title.
            game = "—"
            devices = data.get("devices") or []
            if isinstance(devices, list) and len(devices) > 0:
                for device in devices:
                    if not isinstance(device, dict):
                        continue
                    titles = device.get("titles") or []
                    if isinstance(titles, list) and len(titles) > 0:
                        for title in titles:
                            if isinstance(title, dict) and title.get("placement") == "Full":
                                game = title.get("name") or title.get("titleName") or "—"
                                break
                        if game == "—":
                            for title in titles:
                                if isinstance(title, dict):
                                    if title.get("placement") == "Active" or title.get("state") == "Active":
                                        game = title.get("name") or title.get("titleName") or "—"
                                        break
                        if game == "—":
                            first_title = titles[0]
                            if isinstance(first_title, dict):
                                game = first_title.get("name") or first_title.get("titleName") or "—"
                    if game != "—":
                        break

            primary_device = ""
            if devices and isinstance(devices, list) and devices:
                d0 = devices[0]
                if isinstance(d0, dict):
                    primary_device = d0.get("type", "")

            if not game or str(game).strip() == "":
                game = "—"

            # Historical: last game + when (shown as 'Last seen', never as live).
            last_seen_game = ""
            last_seen_ago = ""
            last_seen_private = False   # was the last-seen MOMENT itself in work hours?
            ls = data.get("lastSeen") or {}
            if isinstance(ls, dict) and (ls.get("titleName") or ls.get("name")):
                last_seen_game = ls.get("titleName") or ls.get("name") or ""
                try:
                    ts_raw = (ls.get("timestamp") or "").split(".")[0]
                    ls_dt = datetime.fromisoformat(ts_raw)     # UTC-naive
                    # Convert the UTC-naive last-seen moment to naive ET, then decide
                    # if it fell inside work hours — this is a property of the MOMENT,
                    # not of the current time, so redaction survives past 5pm.
                    ls_dt_et = ls_dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(_ET).replace(tzinfo=None)
                    last_seen_private = in_work_hours(ls_dt_et)
                    mins = max(0, int((datetime.utcnow() - ls_dt).total_seconds() / 60))
                    if mins < 60:
                        last_seen_ago = f"{mins}m ago"
                    elif mins < 48 * 60:
                        last_seen_ago = f"{mins // 60}h ago"
                    else:
                        last_seen_ago = f"{mins // 1440}d ago"
                except Exception:
                    last_seen_ago = ""

            # Truthful presence: Online ONLY when a device is actually active.
            # (Social `state` under-reports; lastSeen must never over-report.)
            device_active = any(isinstance(d, dict) and d.get("titles") for d in devices)
            presence_state = "Online" if device_active else (raw_state or "Offline")
            playing_now = device_active and game != "—" and is_real_game(game)

            # Profile from player/summary — the /account endpoint returns empty
            # settings for this account, but player/summary/{xuid} has the real
            # gamerscore, gamerpic, reputation, and gamertag.
            gamerpic = ""
            gamerscore = 0
            tenure = 0
            gamertag = XBL_GAMERTAG
            real_name = ""
            account_tier = ""
            reputation = ""
            try:
                note_api_call()
                sum_res = await client.get(f"https://xbl.io/api/v2/player/summary/{xuid}", headers=headers, timeout=10)
                if sum_res.status_code == 200:
                    people = ((sum_res.json() or {}).get("content") or {}).get("people") or []
                    if people:
                        pp = people[0]
                        gamertag = pp.get("gamertag") or gamertag
                        gamerpic = pp.get("displayPicRaw") or ""
                        real_name = pp.get("realName") or ""
                        reputation = pp.get("xboxOneRep") or ""
                        try:
                            gamerscore = int(pp.get("gamerScore") or 0)
                        except Exception:
                            gamerscore = 0
            except Exception as e:
                print(f"Xbox player summary fetch error: {e}")

            # Log meaningful game changes — LIVE device activity only (lastSeen is
            # history and must never masquerade as a launch).
            if playing_now and game and game != "—":
                try:
                    log = persistence.load_xbox_log()
                    last_entry = log[0] if log else None
                    if not last_entry or last_entry.get("game") != game:
                        entry = {
                            "game": game,
                            "timestamp": _now_et().isoformat(),
                            "device": primary_device or "",
                        }
                        log.insert(0, entry)
                        log = log[:100]  # keep last 100
                        persistence.save_xbox_log(log)
                except Exception as e:
                    print(f"Xbox game log error: {e}")

            # Success path: update the shared cache (with timestamp) and return fresh data.
            # Future calls within XBOX_CACHE_TTL_SECONDS will be served from this without touching xbl.io.
            fetch_time = time.time()
            state.last_xbox_data = {
                "status": "ok",
                "state": presence_state,
                "game": game,
                "playing_now": playing_now,
                "kind": classify_title(game) if playing_now else "",
                "device": _pretty_device(primary_device),
                "last_seen_game": _clean_title(last_seen_game) if last_seen_game else "",
                "last_seen_ago": last_seen_ago,
                "last_seen_private": last_seen_private,
                "gamertag": gamertag,
                "gamerpic": gamerpic,
                "gamerscore": gamerscore,
                "tenure": tenure,
                "real_name": real_name,
                "account_tier": account_tier,
                "reputation": reputation,
                "social_state": raw_state,
                "xuid": xuid or XBL_XUID,
                "last_updated": fetch_time
            }
            state._last_xbox_fetch_time = fetch_time
            return state.last_xbox_data

        except Exception as e:
            print(f"Xbox API error: {e}")
            return state.last_xbox_data


# ── Games library (real cover art + achievements + last-played) ──────────────
# achievements/player/{xuid} returns the full title history with real box art,
# achievement completion, gamerscore per game, and last-played timestamps —
# richer and more truthful than sniffing presence. Cached (changes slowly).
_title_history_cache = {"data": [], "ts": 0.0}
TITLE_HISTORY_TTL = 1800   # 30 min


async def get_title_history():
    if not XBL_API_KEY or XBL_API_KEY in XBL_PLACEHOLDER_KEYS or not XBL_XUID:
        return _title_history_cache["data"]
    now = time.time()
    if _title_history_cache["data"] and now - _title_history_cache["ts"] < TITLE_HISTORY_TTL:
        return _title_history_cache["data"]
    headers = {"X-Authorization": XBL_API_KEY, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient() as client:
            note_api_call()
            res = await client.get(f"https://xbl.io/api/v2/achievements/player/{XBL_XUID}", headers=headers, timeout=12)
            if res.status_code != 200:
                return _title_history_cache["data"]
            titles = ((res.json() or {}).get("content") or {}).get("titles") or []
            games = []
            for t in titles:
                if not isinstance(t, dict) or t.get("type") != "Game":
                    continue
                if not is_real_game(t.get("name", "")):
                    continue
                a = t.get("achievement") or {}
                th = t.get("titleHistory") or {}
                games.append({
                    "name": _clean_title(t.get("name", "")),
                    "titleId": str(t.get("titleId") or ""),
                    "image": t.get("displayImage") or "",
                    "gamerscore": a.get("currentGamerscore") or 0,
                    "total_gamerscore": a.get("totalGamerscore") or 0,
                    "achievements": a.get("currentAchievements") or 0,
                    "total_achievements": a.get("totalAchievements") or 0,
                    "progress": a.get("progressPercentage") or 0,
                    "last_played": th.get("lastTimePlayed") or "",
                })
            games.sort(key=lambda gm: gm.get("last_played") or "", reverse=True)
            _title_history_cache.update({"data": games, "ts": now})
    except Exception as e:
        print(f"Xbox title history error: {e}")
    return _title_history_cache["data"]


def _name_tokens(name: str) -> set:
    n = _clean_title(name).lower()
    for ch in "()[]|:.,":
        n = n.replace(ch, " ")
    # drop generic platform/edition words that bloat matches
    stop = {"xbox", "series", "x", "s", "game", "preview", "edition", "the", "one"}
    return {t for t in n.split() if t and t not in stop}


def migrate_sessions_et():
    """One-time repair: sessions recorded before the Eastern-time fix were stamped
    in UTC (Railway's clock), so their start/end read 4-5h off — which made the
    day's story illogical. Convert any un-tagged session from UTC to ET and tag
    it; brand-new sessions are already ET and just get tagged. Idempotent."""
    sessions = persistence.load_xbox_sessions()
    if not sessions or all(s.get("tz") == "et" for s in sessions):
        return
    changed = False
    for s in sessions:
        if s.get("tz") == "et":
            continue
        for k in ("start", "end"):
            try:
                dt = datetime.fromisoformat(s[k]).replace(tzinfo=ZoneInfo("UTC")).astimezone(_ET).replace(tzinfo=None)
                s[k] = dt.isoformat()
            except Exception:
                pass
        s["tz"] = "et"
        changed = True
    if changed:
        persistence.save_xbox_sessions(sessions)


# ── Achievements: real in-game moments with exact unlock times ───────────────
# Xbox exposes no live game state (no FIFA scores), but per-title achievements
# carry an exact unlock timestamp + gamerscore — the truest 'something happened
# in-game at this minute' signal available. Pinned onto the day's HR curve.
_ach_cache = {"data": [], "ts": 0.0}
ACH_TTL = 1800


async def get_recent_achievements(library: list, days: int = 4, cap_titles: int = 6):
    now_ts = time.time()
    if _ach_cache["data"] and now_ts - _ach_cache["ts"] < ACH_TTL:
        return _ach_cache["data"]
    if not XBL_API_KEY or XBL_API_KEY in XBL_PLACEHOLDER_KEYS or not XBL_XUID:
        return _ach_cache["data"]
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    recent_titles = [g for g in (library or [])
                     if g.get("titleId") and (g.get("last_played") or "")[:10] >= cutoff][:cap_titles]
    headers = {"X-Authorization": XBL_API_KEY, "Accept": "application/json"}
    out = []
    try:
        async with httpx.AsyncClient() as client:
            for g in recent_titles:
                if api_calls_last_hour() >= API_BUDGET_PER_HOUR:
                    break
                note_api_call()
                try:
                    res = await client.get(
                        f"https://xbl.io/api/v2/achievements/player/{XBL_XUID}/{g['titleId']}",
                        headers=headers, timeout=12)
                    if res.status_code != 200:
                        continue
                    ach = ((res.json() or {}).get("content") or {}).get("achievements") or []
                except Exception:
                    continue
                for a in ach:
                    if a.get("progressState") != "Achieved":
                        continue
                    tu = (a.get("progression") or {}).get("timeUnlocked") or ""
                    if not tu or tu[:4] < "2020":
                        continue
                    try:
                        dt = datetime.fromisoformat(tu.replace("Z", "+00:00")).astimezone(_ET).replace(tzinfo=None)
                    except Exception:
                        continue
                    gs = next((r.get("value") for r in (a.get("rewards") or [])
                               if r.get("type") == "Gamerscore"), 0)
                    rare = (a.get("rarity") or {}).get("currentPercentage")
                    out.append({
                        "game": g["name"], "image": g.get("image", ""),
                        "name": a.get("name") or "Achievement",
                        "gs": int(gs or 0), "dt": dt, "ts": dt.isoformat(),
                        "rare": float(rare) if rare is not None else None,
                    })
        out.sort(key=lambda x: x["dt"], reverse=True)
        _ach_cache.update({"data": out, "ts": now_ts})
    except Exception as e:
        print(f"achievements fetch error: {e}")
    return _ach_cache["data"]


def recent_unlocks_display(achievements: list, limit: int = 8, reveal: bool = False) -> list:
    out = []
    src = achievements if reveal else [a for a in achievements if not in_work_hours(a.get("dt"))]
    for a in src[:limit]:
        out.append({**a, "when": a["dt"].strftime("%b %-d · %-I:%M %p").replace(" 0", " "),
                    "rare_txt": (f"{a['rare']:.0f}% have this" if a.get("rare") and a["rare"] <= 20 else "")})
    return out


def find_cover(games: list, name: str) -> str:
    """Best-effort match of a game name to its real cover image. Presence names
    ('EA FC 26 (XSX)') and library names ('EA SPORTS FC 26 Xbox Series X|S')
    differ, so match by shared meaningful tokens, best overlap wins."""
    if not name or not games:
        return ""
    target = _name_tokens(name)
    if not target:
        return ""
    best_img, best_score = "", 0
    for gm in games:
        overlap = len(target & _name_tokens(gm.get("name", "")))
        if overlap > best_score:
            best_score, best_img = overlap, gm.get("image", "")
    return best_img if best_score >= 1 else ""


def _scores_by_name(library: list) -> dict:
    """Highest gamerscore per game name — collapses duplicate library entries
    (same title listed under multiple platforms) so deltas can't go phantom."""
    best = {}
    for g in library or []:
        nm = g.get("name")
        if not nm:
            continue
        gs = int(g.get("gamerscore") or 0)
        if nm not in best or gs > best[nm]:
            best[nm] = gs
    return best


def record_xbox_snapshot(gamerscore: int, library: list) -> None:
    """Capture today's gamerscore + per-game scores into the daily history
    (idempotent per calendar day). This is what turns the static library into a
    real progress record we can compute momentum from."""
    try:
        scores = _scores_by_name(library)
        if not gamerscore and not scores:
            return
        from datetime import date as _date
        entry = {
            "day": _date.today().isoformat(),
            "recorded_at": _now_et().isoformat(),
            "total_gamerscore": int(gamerscore or 0),
            "games_tracked": len(library),
            "total_achievements": sum(int(g.get("achievements") or 0) for g in library),
            "scores": scores,
        }
        persistence.upsert_xbox_snapshot(entry)
    except Exception as e:
        print(f"xbox snapshot record error: {e}")


def compute_progress(history: list, library: list, gamerscore: int) -> dict:
    """Momentum from the daily history: gamerscore change over 7/30 days, which
    games you're actively earning in, and a trend series for a sparkline."""
    from datetime import date as _date, timedelta as _td
    out = {
        "has_history": False, "total_gamerscore": int(gamerscore or 0),
        "delta_7d": None, "delta_30d": None, "earning_lately": [], "trend": [],
    }
    hist = sorted([h for h in (history or []) if h.get("day")], key=lambda h: h["day"])
    if not hist:
        return out
    out["has_history"] = len(hist) >= 2
    today = _date.today()

    def total_on_or_before(cutoff_iso):
        prior = [h for h in hist if h["day"] <= cutoff_iso]
        return prior[-1] if prior else (hist[0] if hist else None)

    base7 = total_on_or_before((today - _td(days=7)).isoformat())
    base30 = total_on_or_before((today - _td(days=30)).isoformat())
    cur = int(gamerscore or 0)
    if base7 and base7.get("total_gamerscore"):
        out["delta_7d"] = cur - int(base7["total_gamerscore"])
    if base30 and base30.get("total_gamerscore"):
        out["delta_30d"] = cur - int(base30["total_gamerscore"])

    # Per-game gains vs the oldest snapshot within ~30 days (or the very first).
    # A baseline recorded TODAY has no elapsed time, so there can be no real gains.
    baseline = base30 or hist[0]
    if baseline.get("day") and baseline["day"] < today.isoformat():
        past_scores = baseline.get("scores") or {}
        meta_by_name = {g["name"]: g for g in library if g.get("name")}
        gains = []
        for nm, now_gs in _scores_by_name(library).items():
            was = int(past_scores.get(nm, now_gs))
            if now_gs > was:
                g = meta_by_name.get(nm, {})
                gains.append({
                    "name": nm, "gain": now_gs - was, "image": g.get("image", ""),
                    "progress": g.get("progress", 0), "gamerscore": now_gs,
                    "total_gamerscore": g.get("total_gamerscore", 0),
                })
        gains.sort(key=lambda x: x["gain"], reverse=True)
        out["earning_lately"] = gains[:5]

    out["trend"] = [{"day": h["day"], "gs": int(h.get("total_gamerscore") or 0)}
                    for h in hist if h.get("total_gamerscore")]
    return out


# ── The Story engine ─────────────────────────────────────────────────────────
# Cross-references the logs the site already keeps (true play sessions, daily
# vitals, daily gamerscore, the games library) into personal narration:
# Body × Controller, gaming eras & comebacks, and a Week in Review. Everything
# is computed from observed data and degrades honestly while logs are young.

def _parse_sessions(sessions: list) -> list:
    out = []
    for s in sessions or []:
        if not is_real_game(s.get("game", "")):
            continue
        try:
            start = datetime.fromisoformat(s["start"])
            end = datetime.fromisoformat(s["end"])
        except Exception:
            continue
        dur = max(300.0, (end - start).total_seconds())
        out.append({"game": _clean_title(s["game"]), "start": start, "end": end, "dur": dur})
    return out


def compute_body_controller(sessions: list, vitals_history: list) -> list:
    """Cross the session log with the daily vitals log. Only speaks when both
    sides have enough samples — never invents a correlation."""
    sess = _parse_sessions(sessions)
    vh = {v.get("day"): v for v in (vitals_history or []) if v.get("day")}
    lines = []
    if not sess or len(vh) < 4:
        return lines

    # A late session (starting 22:00–04:00) affects the sleep score recorded the
    # NEXT calendar morning; a session before 22:00 affects that same night too.
    late_nights = set()
    gaming_days = set()
    for s in sess:
        gaming_days.add(s["start"].date().isoformat())
        h = s["start"].hour
        if h >= 22:
            late_nights.add((s["start"].date() + timedelta(days=1)).isoformat())
        elif h < 4:
            late_nights.add(s["start"].date().isoformat())

    def avg(vals):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return sum(vals) / len(vals) if vals else None

    late_sleep = avg([vh[d].get("sleep") for d in late_nights if d in vh])
    other_sleep = avg([v.get("sleep") for d, v in vh.items() if d not in late_nights])
    n_late = len([d for d in late_nights if d in vh and vh[d].get("sleep") is not None])
    if late_sleep is not None and other_sleep is not None and n_late >= 3:
        diff = round(late_sleep - other_sleep)
        if diff <= -3:
            lines.append(f"Late-night sessions cost you sleep — you score **{abs(diff)} points lower** the morning after ({n_late} nights measured).")
        elif diff >= 3:
            lines.append(f"Oddly, you sleep **{diff} points better** after late sessions ({n_late} nights measured). Whatever works.")
        else:
            lines.append(f"Late-night gaming barely moves your sleep — within {abs(diff)} points across {n_late} measured nights.")

    game_ready = avg([v.get("readiness") for d, v in vh.items() if d in gaming_days])
    rest_ready = avg([v.get("readiness") for d, v in vh.items() if d not in gaming_days])
    n_game = len([d for d in gaming_days if d in vh and vh[d].get("readiness") is not None])
    n_rest = len([d for d, v in vh.items() if d not in gaming_days and v.get("readiness") is not None])
    if game_ready is not None and rest_ready is not None and n_game >= 3 and n_rest >= 3:
        diff = round(game_ready - rest_ready)
        if diff >= 3:
            lines.append(f"Gaming days run **{diff} readiness points higher** than off days — the controller might be recovery for you.")
        elif diff <= -3:
            lines.append(f"Gaming days run **{abs(diff)} readiness points lower** than off days — worth watching.")

    return lines


def compute_eras(sessions: list, legacy_log: list, library: list) -> list:
    """Narrate your gaming phases: the current era, the one it replaced, and
    comebacks (returning to a game untouched for 30+ days)."""
    from datetime import date as _date
    lines = []
    today = _date.today()

    # Day -> dominant game, from true sessions first, else the launch log.
    day_game: dict = {}
    for s in _parse_sessions(sessions):
        d = s["start"].date().isoformat()
        day_game.setdefault(d, {})
        day_game[d][s["game"]] = day_game[d].get(s["game"], 0) + s["dur"]
    for e in legacy_log or []:
        name = e.get("game", "")
        if not is_real_game(name):
            continue
        try:
            d = datetime.fromisoformat((e.get("timestamp") or "").replace("Z", "+00:00")).date().isoformat()
        except Exception:
            continue
        day_game.setdefault(d, {})
        day_game[d][_clean_title(name)] = day_game[d].get(_clean_title(name), 0) + 1

    if len(day_game) < 4:
        return lines
    days = sorted(day_game)
    dominant = {d: max(day_game[d], key=day_game[d].get) for d in days}

    # Current era: most-dominant game across the last 14 active days.
    recent = [dominant[d] for d in days[-14:]]
    cur = Counter(recent).most_common(1)[0][0]
    era_days = [d for d in days if dominant[d] == cur]
    first_day = min(era_days)
    try:
        span = (today - _date.fromisoformat(first_day)).days + 1
    except Exception:
        span = len(era_days)
    prior = [dominant[d] for d in days if d < first_day]
    if prior:
        prev = Counter(prior).most_common(1)[0][0]
        if prev != cur:
            lines.append(f"You're in your **{cur} era** — {span} days and counting. It dethroned {prev}.")
        else:
            lines.append(f"The **{cur} era** is {span} days old and unchallenged.")
    else:
        lines.append(f"You're in your **{cur} era** — {span} days and counting.")

    # Comebacks: a session for a game whose library last-played gap was 30+ days.
    lib_last = {}
    for g in library or []:
        lp = g.get("last_played") or ""
        try:
            lib_last[g["name"]] = datetime.fromisoformat(lp.replace("Z", "+00:00")).date()
        except Exception:
            continue
    seen_days = {}
    for d in days:
        for gm in day_game[d]:
            seen_days.setdefault(gm, []).append(d)
    for gm, ds in seen_days.items():
        ds = sorted(ds)
        for i in range(1, len(ds)):
            try:
                gap = (_date.fromisoformat(ds[i]) - _date.fromisoformat(ds[i - 1])).days
            except Exception:
                continue
            if gap >= 30 and ds[i] >= days[-7]:
                lines.append(f"**{gm}** just made a comeback — first session in {gap} days.")
                break

    return lines[:3]


def compute_week_review(sessions: list, history: list) -> dict:
    """The last 7 days as one honest, auto-written paragraph."""
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    week_start = today - _td(days=6)
    sess = [s for s in _parse_sessions(sessions) if s["start"].date() >= week_start]
    out = {"has_data": bool(sess), "text": "", "stats": {}}
    if not sess:
        return out

    total = sum(s["dur"] for s in sess)
    by_game: dict = {}
    for s in sess:
        by_game[s["game"]] = by_game.get(s["game"], 0) + s["dur"]
    top_game, top_secs = max(by_game.items(), key=lambda kv: kv[1])
    days_played = len({s["start"].date() for s in sess})
    marathon = max(sess, key=lambda s: s["dur"])
    latest = max(sess, key=lambda s: (s["start"].hour + 24) % 28)   # 22:00–04:00 ranks latest

    gs_delta = None
    hist = sorted([h for h in (history or []) if h.get("day")], key=lambda h: h["day"])
    base = [h for h in hist if h["day"] <= week_start.isoformat()]
    if base and hist and hist[-1].get("total_gamerscore") and base[-1].get("total_gamerscore"):
        gs_delta = int(hist[-1]["total_gamerscore"]) - int(base[-1]["total_gamerscore"])

    bits = [
        f"**{_fmt_hours(total)}** across {len(sess)} sessions on {days_played} of the last 7 days.",
        f"**{top_game}** owned the week with {_fmt_hours(top_secs)}.",
        f"Longest sitting: **{_fmt_duration(marathon['dur'])}** ({marathon['game']}).",
    ]
    lh = latest["start"].hour
    if lh >= 22 or lh < 4:
        bits.append(f"Latest start: **{latest['start'].strftime('%-I:%M %p').lower()}** — night shift.")
    if gs_delta and gs_delta > 0:
        bits.append(f"Gamerscore up **+{gs_delta:,} G**.")
    out["text"] = " ".join(bits)
    out["stats"] = {"hours": _fmt_hours(total), "sessions": len(sess), "top_game": top_game, "gs_delta": gs_delta}
    return out


# ── The Pulse: heart rate joined to play sessions ────────────────────────────
# On a periodic sweep, closed sessions get Oura heart-rate stats for their exact
# window written PERMANENTLY into the session record (hr_avg/hr_peak/hr_n).
# Oura's API only reaches back ~2 weeks — computing this at page-load would let
# older sessions lose their pulse forever. Log first, chart second.
HR_MIN_SAMPLES = 4          # never claim a session HR from fewer readings
HR_SWEEP_INTERVAL = 900     # sweep at most every 15 min (client caches the call)
_hr_sweep_last = 0.0


async def sweep_session_hr(force: bool = False):
    global _hr_sweep_last
    now_ts = time.time()
    if not force and now_ts - _hr_sweep_last < HR_SWEEP_INTERVAL:
        return
    _hr_sweep_last = now_ts
    if not state.oura_client:
        return
    sessions = persistence.load_xbox_sessions()
    now = _now_et()

    # One-time recovery: sessions swept before the datetime-fetch fix were marked
    # done with NO heart rate (the old fetch returned the wrong window). Oura still
    # holds ~14 days, so re-open those for a single retry under the fixed fetch.
    reach = now - timedelta(days=13, hours=12)
    for s in sessions:
        if s.get("closed") and not s.get("hr_avg") and not s.get("hr_v2"):
            try:
                if datetime.fromisoformat(s["end"]) >= reach:
                    s["hr_done"] = False        # try again with the correct fetch
            except Exception:
                pass

    pending = []
    for s in sessions:
        if not s.get("closed") or s.get("hr_done"):
            continue
        try:
            end = datetime.fromisoformat(s["end"])
        except Exception:
            s["hr_done"] = True
            continue
        if (now - end).total_seconds() < 1800:
            continue                        # let Oura sync before judging
        pending.append(s)
    if not pending:
        persistence.save_xbox_sessions(sessions)
        return

    try:
        start_dt = (_now_et() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S-04:00")
        end_dt = _now_et().strftime("%Y-%m-%dT%H:%M:%S-04:00")
        raw = await state.oura_client.get_heartrate_range(start_dt, end_dt)
    except Exception as e:
        print(f"pulse sweep HR fetch error: {e}")
        return
    pts = []
    for e in raw or []:
        if not isinstance(e, dict) or e.get("bpm") is None or not e.get("timestamp"):
            continue
        try:
            t = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
            pts.append((t.astimezone(_ET).replace(tzinfo=None), e["bpm"]))
        except Exception:
            continue
    pts.sort()

    oldest_reach = datetime.combine(date.today() - timedelta(days=13), datetime.min.time())
    changed = False
    for s in pending:
        try:
            st = datetime.fromisoformat(s["start"])
            en = datetime.fromisoformat(s["end"])
        except Exception:
            s["hr_done"] = True
            changed = True
            continue
        window = [b for t, b in pts if st <= t <= en]
        if len(window) >= HR_MIN_SAMPLES:
            s["hr_avg"] = round(sum(window) / len(window))
            s["hr_peak"] = max(window)
            s["hr_n"] = len(window)
            s["hr_done"] = True
            s["hr_v2"] = True
            changed = True
        elif (now - en).total_seconds() > 24 * 3600 or st < oldest_reach:
            s["hr_done"] = True             # window has passed — no honest HR exists
            s["hr_v2"] = True               # retried under the fixed fetch; don't loop
            changed = True
    if changed:
        persistence.save_xbox_sessions(sessions)


def compute_pulse_story(sessions: list) -> list:
    """Per-game heart-rate narration — only from sessions with real HR stats."""
    by_game: dict = {}
    for s in sessions or []:
        if s.get("hr_avg") and is_real_game(s.get("game", "")):
            by_game.setdefault(_clean_title(s["game"]), []).append(s["hr_avg"])
    lines = []
    means = {g: sum(v) / len(v) for g, v in by_game.items() if len(v) >= 3}
    if len(means) >= 2:
        hot = max(means, key=means.get)
        cool = min(means, key=means.get)
        gap = round(means[hot] - means[cool])
        if gap >= 5:
            lines.append(f"**{hot}** runs your heart **{gap} bpm hotter** than {cool} ({round(means[hot])} vs {round(means[cool])} avg).")
    if means:
        cool = min(means, key=means.get)
        if len(means) == 1 or not lines:
            lines.append(f"Calmest gaming: **{cool}** at {round(means[cool])} bpm average.")
    peak_s = max((s for s in sessions or [] if s.get("hr_peak")), key=lambda s: s["hr_peak"], default=None)
    if peak_s:
        lines.append(f"Peak recorded mid-game: **{peak_s['hr_peak']} bpm** during {_clean_title(peak_s['game'])}.")
    return lines[:3]


def compute_pulse_chart(sessions: list, history: list, days: int = 14) -> dict:
    """Data for The Pulse chart: daily play hours (bars), daily avg session HR
    (line, honest gaps), and the day's top game for tooltips."""
    today = date.today()
    day_list = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    hours = {d: 0.0 for d in day_list}
    hr_vals: dict = {d: [] for d in day_list}
    top: dict = {d: {} for d in day_list}
    for s in _parse_sessions(sessions):
        d = s["start"].date().isoformat()
        if d not in hours:
            continue
        hours[d] += s["dur"] / 3600
        top[d][s["game"]] = top[d].get(s["game"], 0) + s["dur"]
    for s in sessions or []:
        if not s.get("hr_avg"):
            continue
        try:
            d = datetime.fromisoformat(s["start"]).date().isoformat()
        except Exception:
            continue
        if d in hr_vals:
            hr_vals[d].append(s["hr_avg"])
    return {
        "labels": [datetime.fromisoformat(d).strftime("%b %-d") for d in day_list],
        "hours": [round(hours[d], 2) if hours[d] else 0 for d in day_list],
        "hr": [round(sum(v) / len(v)) if (v := hr_vals[d]) else None for d in day_list],
        "top_game": [max(top[d], key=top[d].get) if top[d] else "" for d in day_list],
        "has_hr": any(hr_vals[d] for d in day_list),
        "has_play": any(hours[d] for d in day_list),
    }


# ── The Replay: yesterday's full HR curve annotated with your day ────────────
# Oura is a historian, not a live wire — its detailed heart-rate curve lands a
# day later. So we replay it: the complete curve for yesterday with game
# sessions, sleep, and workouts shaded onto the exact minutes they happened.

async def compute_day_replay(achievements=None, day_offset=1, reveal=False):
    """A day rendered on a true minutes-since-midnight axis: the full HR curve
    plus game/media/workout/sleep bands and achievement markers at their exact
    times. The frontend defaults its view to the ACTIVE window (when you were
    actually doing something) and lets you scroll/zoom out to the whole day."""
    if not state.oura_client:
        return {"has_data": False}
    migrate_sessions_et()
    day = _now_et().date() - timedelta(days=day_offset)
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    def mins(dt):
        return max(0.0, min(1440.0, (dt - day_start).total_seconds() / 60))

    # Full-day HR curve — MUST use the datetime endpoint (date params return only
    # a recent slice, which is what made the old chart show ~3 evening hours).
    try:
        raw = await state.oura_client.get_heartrate_range(
            day_start.strftime("%Y-%m-%dT%H:%M:%S-04:00"),
            day_end.strftime("%Y-%m-%dT%H:%M:%S-04:00"))
    except Exception:
        raw = []
    pts = []
    for e in raw or []:
        if not isinstance(e, dict) or e.get("bpm") is None or not e.get("timestamp"):
            continue
        try:
            t = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone(_ET).replace(tzinfo=None)
        except Exception:
            continue
        if day_start <= t < day_end:
            pts.append((t, e["bpm"]))
    pts.sort()
    if len(pts) < 12:
        return {"has_data": False, "day": day.strftime("%A, %b %-d")}
    step = max(1, len(pts) // 288)
    pts = pts[::step]
    points = [{"x": round(mins(t), 1), "y": b} for t, b in pts]
    bpm = [b for _, b in pts]
    tmins = [mins(t) for t, _ in pts]

    def hr_at(m):
        """Nearest bpm reading to a given minute."""
        best_i, best_d = 0, 1e9
        for i, tm in enumerate(tmins):
            d = abs(tm - m)
            if d < best_d:
                best_d, best_i = d, i
        return bpm[best_i]

    def clk(m):
        h, mm = divmod(int(m), 60)
        h %= 24
        ap = "AM" if h < 12 else "PM"
        hh = h % 12 or 12
        return f"{hh}:{mm:02d} {ap}"

    bands = []
    game_facts = []
    activity_spans = []   # (start_min, end_min) for computing the active window
    hidden_count = 0      # work-hours game/media/unlock items withheld from the public view
    sleep_hidden_count = 0  # sleep periods hidden from public (always private, not work-hours)

    # Game + media sessions overlapping the day, classified from the stored title.
    for s in persistence.load_xbox_sessions():
        kind = classify_title(s.get("game", ""))
        if not kind:
            continue
        try:
            st = datetime.fromisoformat(s["start"])
            en = datetime.fromisoformat(s["end"])
        except Exception:
            continue
        if en <= day_start or st >= day_end:
            continue
        if not reveal and in_work_hours(st):
            hidden_count += 1               # private during work hours
            continue
        name = _clean_title(s["game"])
        label = name + (f" · ♥{s['hr_avg']}" if s.get("hr_avg") else "")
        x0, x1 = mins(max(st, day_start)), mins(min(en, day_end))
        bands.append({"kind": kind, "label": label, "x0": round(x0, 1), "x1": round(x1, 1)})
        activity_spans.append((x0, x1))
        if kind == "game":
            game_facts.append({"name": name, "dur": (min(en, day_end) - max(st, day_start)).total_seconds(),
                               "hr": s.get("hr_avg"), "start": max(st, day_start)})

    # Sleep (longest = 'asleep', others 'nap').
    try:
        sleeps = await state.oura_client.get_sleep(
            (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat())
    except Exception:
        sleeps = []
    sleep_periods = []
    for sl in sleeps or []:
        try:
            bs = datetime.fromisoformat(sl["bedtime_start"].replace("Z", "+00:00")).astimezone(_ET).replace(tzinfo=None)
            be = datetime.fromisoformat(sl["bedtime_end"].replace("Z", "+00:00")).astimezone(_ET).replace(tzinfo=None)
        except Exception:
            continue
        if be <= day_start or bs >= day_end:
            continue
        sleep_periods.append({"bs": bs, "be": be, "dur": sl.get("total_sleep_duration") or 0})
    main_sleep = None
    if sleep_periods:
        longest = max(sleep_periods, key=lambda p: p["dur"])
        main_sleep = {"dur": longest["dur"], "wake": longest["be"]}
        for p in sleep_periods:
            is_main = p is longest
            if not is_main and (p["be"] - p["bs"]).total_seconds() < 1200:
                continue
            # Sleep is private, full stop: no asleep/nap bands in the public view.
            # (A workday wake time or an afternoon nap is nobody's business —
            # sleep lives on the vitals page; the owner sees it when revealed.)
            if not reveal:
                sleep_hidden_count += 1
                continue
            bands.append({"kind": "sleep", "label": "asleep" if is_main else "nap",
                          "x0": round(mins(p["bs"]), 1), "x1": round(mins(p["be"]), 1)})

    # Workouts.
    try:
        workouts = await state.oura_client.get_workouts(day.isoformat(), (day + timedelta(days=1)).isoformat())
    except Exception:
        workouts = []
    for w in workouts or []:
        try:
            ws = datetime.fromisoformat(w["start_time"].replace("Z", "+00:00")).astimezone(_ET).replace(tzinfo=None)
            we = datetime.fromisoformat(w["end_time"].replace("Z", "+00:00")).astimezone(_ET).replace(tzinfo=None)
        except Exception:
            continue
        if we <= day_start or ws >= day_end or (we - ws).total_seconds() < 300:
            continue
        act = (w.get("activity") or "workout").replace("_", " ")
        x0, x1 = mins(ws), mins(we)
        bands.append({"kind": "workout", "label": act, "x0": round(x0, 1), "x1": round(x1, 1)})
        activity_spans.append((x0, x1))

    # Achievement markers at their exact minute (work-hours unlocks stay private).
    moments, day_unlocks = [], []
    for a in achievements or []:
        if day_start <= a["dt"] < day_end:
            if not reveal and in_work_hours(a["dt"]):
                hidden_count += 1
                continue
            day_unlocks.append(a)
            m = mins(a["dt"])
            moments.append({"x": round(m, 1), "label": f"🏆 {a['name']} (+{a['gs']}G · {a['game']})", "hr": hr_at(m)})
            # Note: markers do NOT widen the active window — a lone 2am unlock
            # shouldn't stretch the view across the whole day. The window keys off
            # continuous activity (sessions/workouts); markers ride along and are
            # reachable by zooming out.
    day_unlocks.sort(key=lambda a: a["dt"])
    moments = moments[:14]

    # Active window: the stretch worth looking at. Pad 30m, snap to the hour,
    # min 3h wide. Falls back to waking window (post-wake → last reading).
    if activity_spans:
        lo = min(s for s, _ in activity_spans) - 30
        hi = max(e for _, e in activity_spans) + 30
    elif main_sleep:
        lo = mins(main_sleep["wake"]) - 15
        hi = tmins[-1] + 15
    else:
        lo, hi = tmins[0], tmins[-1]
    lo = max(0, (int(lo) // 60) * 60)
    hi = min(1440, -(-int(hi) // 60) * 60)
    if hi - lo < 180:
        hi = min(1440, lo + 180)
    view = {"start": lo, "end": hi}

    # Facts — computed from the SAME stored session HR the bands show (no more
    # 89-vs-65 contradiction from recomputing over a partial curve).
    peak_i = max(range(len(bpm)), key=lambda i: bpm[i])
    sleep_ranges = [(b["x0"], b["x1"]) for b in bands if b["kind"] == "sleep"]
    def is_sleep(m):
        return any(a <= m <= b for a, b in sleep_ranges)
    waking = [(tmins[i], bpm[i]) for i in range(len(bpm)) if not is_sleep(tmins[i])] or [(tmins[i], bpm[i]) for i in range(len(bpm))]
    low_m, low_v = min(waking, key=lambda p: p[1])
    facts = [f"peak {bpm[peak_i]} bpm at {clk(tmins[peak_i])}"]
    game_hrs = [g["hr"] for g in game_facts if g.get("hr")]
    if game_hrs:
        facts.append(f"in-game avg {round(sum(game_hrs)/len(game_hrs))} bpm")
    facts.append(f"calmest waking {low_v} bpm at {clk(low_m)}")

    # Narration.
    bits = []
    if reveal and main_sleep and main_sleep["dur"]:
        bits.append(f"Slept **{_fmt_duration(main_sleep['dur'])}**, up at {main_sleep['wake'].strftime('%-I:%M %p').lower()}.")
    if game_facts:
        game_facts.sort(key=lambda g: g["start"])
        parts = []
        for g in game_facts[:3]:
            p = f"**{g['name']}** {g['start'].strftime('%-I:%M %p').lower()} ({_fmt_duration(g['dur'])}"
            if g.get("hr"):
                p += f", ♥{g['hr']}"
            parts.append(p + ")")
        bits.append("Played " + ", then ".join(parts) + ".")
    media_bands = [b for b in bands if b["kind"] == "media"]
    if media_bands:
        bits.append(f"Also watching **{media_bands[0]['label'].split(' · ')[0]}**.")
    if day_unlocks:
        total_g = sum(a["gs"] for a in day_unlocks)
        lead = day_unlocks[0]
        extra = f" and {len(day_unlocks) - 1} more" if len(day_unlocks) > 1 else ""
        bits.append(f"Unlocked **{lead['name']}**{extra} (**+{total_g} G**).")
    bits.append(f"Heart peaked at **{bpm[peak_i]} bpm** ({clk(tmins[peak_i])}); calmest waking {low_v}.")
    narration = " ".join(bits)

    return {
        "has_data": True,
        "day": day.strftime("%A, %b %-d"),
        "points": points, "bands": bands, "moments": moments, "view": view,
        "facts": " · ".join(facts),
        "narration": narration,
        "games_count": len([b for b in bands if b["kind"] == "game"]),
        "media_count": len(media_bands),
        "unlocks_count": len(day_unlocks),
        "hidden_count": hidden_count,
        "sleep_hidden_count": sleep_hidden_count,
        "revealed": reveal,
    }


def recent_games_display(games: list, limit: int = 12) -> list:
    """Recently-played library rows for the page: cover, progress, gamerscore, when."""
    out = []
    for gm in games[:limit]:
        lp = gm.get("last_played") or ""
        when = ""
        if lp:
            try:
                dt = datetime.fromisoformat(lp.replace("Z", "+00:00"))
                when = dt.strftime("%b %-d, %Y")
            except Exception:
                when = lp[:10]
        out.append({**gm, "when": when})
    return out


_ET = ZoneInfo("America/New_York")

# Work-hours privacy: gaming / watching / naps that happen on a weekday between
# these hours are hidden from the PUBLIC view (coworkers), but never deleted —
# the admin can reveal their own full day. Privacy, not a lie: hidden ≠ faked.
WORK_START_MIN = 8 * 60 + 30     # 8:30 AM
WORK_END_MIN = 17 * 60           # 5:00 PM


def in_work_hours(dt) -> bool:
    if dt is None or dt.weekday() >= 5:
        return False
    return WORK_START_MIN <= (dt.hour * 60 + dt.minute) < WORK_END_MIN


def redact_status_for_public(d: dict) -> dict:
    """Public-safe copy of the live status during work hours: no current game,
    no device, no fresh last-seen. `state` falls back to Xbox's OWN social
    presence (what Xbox itself reports — typically Offline under the user's
    privacy settings), so we omit our device-derived override rather than
    invent anything."""
    if not isinstance(d, dict) or d.get("status") != "ok":
        return d

    now_work = in_work_hours(_now_et())
    # (a) live activity is hidden only while we're currently in work hours;
    # (b) the last-seen moment is hidden based on WHEN it happened, independent
    #     of the current time — so a 2:30pm session doesn't leak at 5:30pm, and
    #     a non-work-hours last-seen survives even during work hours.
    redact_live = now_work and (d.get("playing_now") or d.get("game") not in ("", "—", None))
    redact_last_seen = bool(d.get("last_seen_private"))

    if not redact_live and not redact_last_seen:
        return d

    out = dict(d)
    if redact_live:
        out["game"] = "—"
        out["playing_now"] = False
        out["kind"] = ""
        out["device"] = ""
        out["state"] = d.get("social_state") or "Offline"
    if redact_last_seen:
        out["last_seen_game"] = ""
        out["last_seen_ago"] = ""
    return out


def _now_et() -> datetime:
    """Naive Eastern time — the user's clock. Railway containers run UTC, so a
    bare datetime.now() there stamped sessions 4-5h off and skewed every
    night-owl / streak stat. All session timestamps use this."""
    return datetime.now(_ET).replace(tzinfo=None)


# ── Session observer ─────────────────────────────────────────────────────────
# The page-triggered log only records what was on screen when a browser happened
# to be watching — it can't know durations or real streaks. This observer runs
# server-side every few minutes and maintains TRUE sessions (start/end/duration).
# A gap larger than SESSION_GAP_SECONDS between sightings closes the session.
SESSION_GAP_SECONDS = 20 * 60


async def poll_presence_sample():
    """Lightweight presence-only check (1 API call — no account fetch) for the
    background observer. Returns (game, device, presence_state):
      - game: real game name, or '' when idle / on the dashboard / off
      - presence_state: raw 'Online' / 'Away' / 'Offline' (drives the cadence)
    Returns None when the API is unreachable (so we never log a false 'stopped')."""
    if not XBL_API_KEY or XBL_API_KEY in XBL_PLACEHOLDER_KEYS:
        return None
    xuid = XBL_XUID
    if not xuid:
        return None
    headers = {"X-Authorization": XBL_API_KEY, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient() as client:
            note_api_call()
            res = await client.get(f"https://xbl.io/api/v2/{xuid}/presence", headers=headers, timeout=10)
            if res.status_code != 200:
                return None
            data = res.json() or {}
            data = data.get("content") or data
            raw_state = data.get("state", "Offline") or "Offline"
            devices = data.get("devices") or []
            game = ""
            device_type = ""
            for device in devices:
                if not isinstance(device, dict):
                    continue
                for title in (device.get("titles") or []):
                    if isinstance(title, dict) and title.get("placement") == "Full":
                        game = title.get("name") or ""
                        device_type = device.get("type", "") or ""
                        break
                if game:
                    break
            # Device activity beats the misleading social-presence state.
            device_active = any(isinstance(d, dict) and d.get("titles") for d in devices)
            presence_state = "Online" if device_active else raw_state
            # Track games AND media (watching sports/streaming) — both get a heart
            # rate; dashboard/system titles return '' and close the session.
            tracked = game if (is_real_game(game) or is_media_title(game)) else ""
            return (tracked, device_type, presence_state)
    except Exception as e:
        print(f"Xbox presence poll error: {e}")
        return None


def record_presence_sample(game: str, device: str = ""):
    """Fold one observation into the session store — the single source of truth.

    A session is one continuous stretch of playing ONE game. Each 5-min poll that
    still sees the same game extends its `end`; a different game, going idle, or a
    gap longer than SESSION_GAP_SECONDS closes it and (if a game) opens a new one.
    `samples` counts observations so we can tell a real sitting from a one-poll blip.
    """
    migrate_sessions_et()
    now = _now_et()
    sessions = persistence.load_xbox_sessions()
    open_s = sessions[0] if sessions and not sessions[0].get("closed") else None

    if open_s:
        try:
            last_end = datetime.fromisoformat(open_s["end"])
        except Exception:
            last_end = now
        gap = (now - last_end).total_seconds()
        same_game = bool(game) and open_s.get("game") == game

        if same_game and gap <= SESSION_GAP_SECONDS:
            open_s["end"] = now.isoformat()                       # still playing — extend
            open_s["samples"] = open_s.get("samples", 1) + 1
            if device and not open_s.get("device"):
                open_s["device"] = device
            persistence.save_xbox_sessions(sessions[:400])
            return

        if not game and gap <= SESSION_GAP_SECONDS:
            # Brief dashboard/idle detour — leave the session open but DON'T extend
            # its end. If you return to the same game within the gap window it
            # resumes as one session; if you stay idle past it, it closes with the
            # end pinned to your last real in-game moment.
            persistence.save_xbox_sessions(sessions[:400])
            return

        open_s["closed"] = True                                   # stopped / switched / long gap
        open_s = None

    if game and not open_s:
        sessions.insert(0, {
            "game": game, "start": now.isoformat(), "end": now.isoformat(),
            "closed": False, "device": device, "samples": 1, "tz": "et",
            "kind": classify_title(game),
        })

    persistence.save_xbox_sessions(sessions[:400])


# ── Gaming intelligence ──────────────────────────────────────────────────────
# The presence log records EVERY foreground title, including dashboard/app states.
# For a *gaming* page we only count real games — this set is the noise to drop.
NON_GAME_TITLES = {
    # Dashboard / system
    "home", "xbox app", "xbox", "settings", "microsoft store", "store",
    "my games & apps", "media player", "movies & tv", "guide", "—",
    "xbox game bar", "start", "系统", "dashboard", "microsoft edge", "edge",
    # Streaming / media apps (not games)
    "netflix", "youtube", "youtube tv", "hulu", "disney+", "disney plus",
    "max", "hbo max", "prime video", "amazon prime video", "spotify", "twitch",
    "peacocktv", "peacock", "apple tv", "plex", "pandora", "sling tv", "espn",
    "paramount+", "paramount plus", "crunchyroll", "fubotv", "tubi", "pluto tv",
}


# Streaming/media brands whose Xbox app names vary ("HBO Max: TV & Movies",
# "Netflix — watch TV shows", …) — prefix-match these, not exact-match.
_NON_GAME_PREFIXES = (
    "netflix", "youtube", "hulu", "disney", "hbo", "max:", "prime video",
    "amazon prime", "spotify", "twitch", "peacock", "apple tv", "plex",
    "paramount", "crunchyroll", "sling", "espn", "fubo", "tubi", "pluto",
    "pandora", "vudu", "movies anywhere",
    # Physical disc / Blu-ray playback — watching, not gaming.
    "blu-ray", "blu ray", "bluray", "4k blu", "dvd player", "disc",
)


# System/dashboard app name PREFIXES — exact-match alone let variants like
# 'Microsoft Store — ...' be narrated as games.
_SYSTEM_PREFIXES = ("microsoft store", "xbox app", "media player", "my games",
                    "microsoft edge", "settings", "game bar")


def is_real_game(name: str) -> bool:
    if not name:
        return False
    n = name.strip().lower()
    if n in NON_GAME_TITLES:
        return False
    if any(n.startswith(p) for p in _SYSTEM_PREFIXES):
        return False
    return not any(n.startswith(p) for p in _NON_GAME_PREFIXES)


# Streaming/sports apps — watching, not playing. Tracked as their own session
# kind so the Replay can show heart rate while you watch a game (ESPN, etc.).
_MEDIA_PREFIXES = _NON_GAME_PREFIXES + ("dazn", "nfl", "nba", "mlb", "wwe", "ufc.tv", "fubo")


def is_media_title(name: str) -> bool:
    if not name:
        return False
    n = name.strip().lower()
    if n in ("home", "xbox app", "xbox", "settings", "microsoft store", "store",
             "my games & apps", "media player", "guide", "—", "microsoft edge", "edge", "start"):
        return False
    return any(n.startswith(p) for p in _MEDIA_PREFIXES)


def classify_title(name: str) -> str:
    """'game' | 'media' | '' (dashboard/system → ignore)."""
    if is_real_game(name):
        return "game"
    if is_media_title(name):
        return "media"
    return ""


def _clean_title(name: str) -> str:
    """Human-friendly game name: strip ™ ® and platform tags like (XSX)."""
    n = (name or "").replace("™", "").replace("®", "").strip()
    for tag in (" (XSX)", " (Xbox Series X|S)", " (PC)", " (Xbox One)"):
        if n.endswith(tag):
            n = n[: -len(tag)]
    return n.strip()


def game_signature(name: str) -> dict:
    """Deterministic 'cover art' for a title — a stable color signature derived from
    the name, so every game gets its own cinematic look with no external images."""
    seed = (name or "xbox").strip().lower()
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    hue = h % 360
    hue2 = (hue + 45) % 360
    words = [w for w in _clean_title(name).split() if w]
    initials = "".join(w[0] for w in words[:2]).upper() or "XB"
    return {
        "c1": f"hsl({hue}, 62%, 20%)",
        "c2": f"hsl({hue2}, 68%, 9%)",
        "accent": f"hsl({hue}, 85%, 62%)",
        "initials": initials,
    }


def _fmt_hours(seconds: float) -> str:
    h = seconds / 3600
    if h >= 10:
        return f"{h:.0f} hours"
    if h >= 1:
        return f"{h:.1f} hours"
    return f"{max(1, round(seconds / 60))} min"


def _fmt_duration(seconds: float) -> str:
    """Compact play-length for the timeline: '2h 18m', '45m'."""
    mins = int(round(seconds / 60))
    if mins < 1:
        return "<1m"
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


_DEVICE_LABELS = {
    "XboxSeriesX": "Xbox Series X", "XboxSeriesS": "Xbox Series S",
    "Scarlett": "Xbox Series X|S", "XboxOne": "Xbox One", "Xbox360": "Xbox 360",
    "Win32": "PC", "WindowsOneCore": "PC", "PC": "PC", "Android": "Mobile",
    "iOS": "Mobile", "Nintendo": "Cloud", "Cloud": "Cloud Gaming",
}


def _pretty_device(t: str) -> str:
    return _DEVICE_LABELS.get(t, t or "")


def sessions_for_display(sessions: list, limit: int = 40, reveal: bool = False) -> list:
    """Format sessions into a readable timeline (newest first): games AND media
    (watching), when it started, how long, device, heart rate, and whether it's
    live. Media rows are tagged so 'peaked at 110 during a movie' reads clearly.
    Work-hours sessions are withheld from the public view unless revealed."""
    out = []
    for s in sessions or []:
        kind = classify_title(s.get("game", ""))
        if not kind:
            continue
        try:
            start = datetime.fromisoformat(s["start"])
            end = datetime.fromisoformat(s["end"])
        except Exception:
            continue
        if not reveal and in_work_hours(start):
            continue
        dur = max(0.0, (end - start).total_seconds())
        live = not s.get("closed")
        # A single-sample session is a brief blip — show "~5 min" not a fake 0.
        if s.get("samples", 1) <= 1 and dur < 60:
            length = "just started" if live else "~5m"
        else:
            length = _fmt_duration(dur) + (" · live" if live else "")
        out.append({
            "game": _clean_title(s["game"]),
            "kind": kind,
            "started": start.strftime("%b %-d • %-I:%M %p"),
            "length": length,
            "device": _pretty_device(s.get("device", "")),
            "live": live,
            "hr": f"{s['hr_avg']} avg · {s['hr_peak']} peak" if s.get("hr_avg") else "",
        })
        if len(out) >= limit:
            break
    return out


def compute_gaming_insights(raw_log: list, current_game: str = "", sessions: list | None = None) -> dict:
    """Truthful, quirky, statistics-based narration for gaming.

    Two data sources, used honestly for what each can actually prove:
    - TRUE sessions from the server-side observer → durations, playtime, real
      day-streaks ("played every day"). Wording may claim time.
    - The legacy page-triggered change log → only launch *counts*; it can't see
      durations, so no line built from it ever claims time or continuity.
    """
    entries = []
    for e in raw_log or []:
        name = e.get("game", "")
        if not is_real_game(name):
            continue
        try:
            dt = datetime.fromisoformat((e.get("timestamp") or "").replace("Z", "+00:00"))
        except Exception:
            dt = None
        entries.append({"game": _clean_title(name), "dt": dt})

    # Parse true sessions (observer data) — each has start/end → duration.
    sess = []
    for s in sessions or []:
        if not is_real_game(s.get("game", "")):
            continue
        try:
            start = datetime.fromisoformat(s["start"])
            end = datetime.fromisoformat(s["end"])
        except Exception:
            continue
        dur = max(0.0, (end - start).total_seconds())
        # An open session observed only once has ~0 duration; count it as one
        # poll interval so live play registers immediately without inflating.
        if dur == 0:
            dur = 300.0
        sess.append({"game": _clean_title(s["game"]), "start": start, "end": end, "dur": dur})

    total = len(entries)
    out = {
        "has_data": total >= 3 or len(sess) >= 2,
        "sessions": total,
        "true_sessions": len(sess),
        "insights": [], "top_games": [], "stats": {},
    }
    if total == 0 and not sess:
        return out

    counts = Counter(e["game"] for e in entries)
    ranked = counts.most_common()
    distinct = len(counts)

    # Playtime by game (true sessions only — the only honest source of hours).
    time_by_game: dict = {}
    for s in sess:
        time_by_game[s["game"]] = time_by_game.get(s["game"], 0.0) + s["dur"]
    total_play = sum(time_by_game.values())

    # Top games: ranked by real hours when we have them, else by launch count.
    if total_play > 0:
        ranked_time = sorted(time_by_game.items(), key=lambda kv: kv[1], reverse=True)
        out["top_games"] = [
            {"name": g, "count": counts.get(g, 0), "pct": round(100 * t / total_play),
             "time": _fmt_hours(t)}
            for g, t in ranked_time[:5]
        ]
        fav, fav_share = ranked_time[0][0], round(100 * ranked_time[0][1] / total_play)
        fav_basis = "time"
    elif ranked:
        out["top_games"] = [
            {"name": g, "count": c, "pct": round(100 * c / total), "time": None}
            for g, c in ranked[:5]
        ]
        fav, fav_share = ranked[0][0], round(100 * ranked[0][1] / total)
        fav_basis = "count"
    else:
        return out

    dated = [e for e in entries if e["dt"]]
    night_events = [s["start"] for s in sess] or [e["dt"] for e in dated]
    night_n = sum(1 for t in night_events if (t.hour >= 21 or t.hour < 4))

    # True day-streak: only from observer sessions (continuous watching), never
    # from the page log (which only records days someone happened to look).
    sess_days = sorted({s["start"].date() for s in sess})
    longest = cur = 1 if sess_days else 0
    for i in range(1, len(sess_days)):
        if (sess_days[i] - sess_days[i - 1]).days == 1:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 1

    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    busiest_day = None
    day_source = [s["start"] for s in sess] or [e["dt"] for e in dated]
    if day_source:
        day_counts = Counter(t.weekday() for t in day_source)
        busiest_day = weekday_names[day_counts.most_common(1)[0][0]]

    all_days = sorted({t.date() for t in day_source})
    span_days = (all_days[-1] - all_days[0]).days + 1 if len(all_days) >= 2 else 0

    out["stats"] = {
        "distinct_games": distinct or len(time_by_game),
        "favorite": fav,
        "favorite_pct": fav_share,
        "night_pct": round(100 * night_n / len(night_events)) if night_events else 0,
        "longest_streak": longest,
        "busiest_day": busiest_day,
        "span_days": span_days,
        "total_play": _fmt_hours(total_play) if total_play else None,
    }

    ins = out["insights"]

    # Favorite — wording matches what the data can actually prove.
    if fav_basis == "time":
        ins.append(f"**{fav}** owns your playtime — {fav_share}% of your tracked hours.")
    elif fav_share >= 40:
        ins.append(f"**{fav}** is your main game — {fav_share}% of everything you've launched.")
    else:
        ins.append(f"You spread your time around — **{distinct} different games**, led by {fav}.")

    # Real hours (observer only).
    if total_play >= 3600:
        avg = total_play / len(sess)
        ins.append(f"**{_fmt_hours(total_play)}** of real tracked playtime — sessions average {_fmt_hours(avg)}.")
        marathon = max(sess, key=lambda s: s["dur"])
        if marathon["dur"] >= 2.5 * 3600:
            ins.append(f"Longest single sitting: **{_fmt_hours(marathon['dur'])}** of {marathon['game']}.")

    # Night owl.
    if night_events:
        np = out["stats"]["night_pct"]
        if np >= 55:
            ins.append(f"Certified night owl — **{np}%** of your play starts after 9pm.")
        elif np <= 20 and len(night_events) >= 8:
            ins.append(f"Daytime gamer — only {np}% of your play is late-night.")

    # Streak — only claimed from continuous observation.
    if longest >= 3:
        ins.append(f"**{longest} days in a row** with a controller in hand — verified by the session tracker.")

    if busiest_day and len(day_source) >= 8:
        ins.append(f"**{busiest_day}** is your most-played day of the week.")

    # Breadth — honest label per source.
    if span_days >= 14:
        if sess:
            ins.append(f"{len(sess)} true play sessions tracked across **{span_days} days**.")
        else:
            ins.append(f"{total} game launches seen across **{span_days} days**.")

    # Current game context.
    cg = _clean_title(current_game)
    if cg and is_real_game(current_game) and cg != fav:
        t = time_by_game.get(cg)
        if t and t >= 1800:
            ins.append(f"Right now: **{cg}** — {_fmt_hours(t)} logged in it so far.")
        elif counts.get(cg):
            ins.append(f"Right now: **{cg}** — you've been back to it {counts[cg]}× recently.")

    return out


