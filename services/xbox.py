"""Xbox Live status via xbl.io."""

from __future__ import annotations

import hashlib
import time
from collections import Counter, deque
from datetime import datetime

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

            presence_state = data.get("state", "Unknown")

            # Step 3: Extract current game/app name - try multiple paths for robustness
            game = "—"

            # Path 1: devices[0].titles (most common for current activity)
            devices = data.get("devices") or []
            if isinstance(devices, list) and len(devices) > 0:
                for device in devices:
                    if not isinstance(device, dict):
                        continue
                    titles = device.get("titles") or []
                    if isinstance(titles, list) and len(titles) > 0:
                        # Prefer Full placement active title (the main game), then any Active
                        for title in titles:
                            if isinstance(title, dict):
                                if title.get("placement") == "Full" and (title.get("state") == "Active" or title.get("placement") == "Full"):
                                    game = title.get("name") or title.get("titleName") or "—"
                                    break
                        if game == "—":
                            for title in titles:
                                if isinstance(title, dict):
                                    if title.get("placement") == "Active" or title.get("state") == "Active":
                                        game = title.get("name") or title.get("titleName") or "—"
                                        break
                        if game == "—":
                            # fallback to first title
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

            # Path 2: direct lastSeenTitle (some response formats)
            if game == "—" and "lastSeenTitle" in data:
                game = data.get("lastSeenTitle") or "—"

            # Path 3: lastSeen.titleName
            if game == "—":
                last_seen = data.get("lastSeen") or {}
                if isinstance(last_seen, dict):
                    game = last_seen.get("titleName") or last_seen.get("name") or "—"

            # Path 4: other possible top-level fields (handle different formats)
            if game == "—":
                game = (
                    data.get("titleName")
                    or data.get("name")
                    or (data.get("title") or {}).get("name") if isinstance(data.get("title"), dict) else data.get("title")
                    or "—"
                )

            # Handle empty / falsy game
            if not game or str(game).strip() == "":
                game = "—"

            # Fetch account profile for gamerpic, gamerscore, tenure (static profile info)
            gamerpic = ""
            gamerscore = 0
            tenure = 0
            gamertag = XBL_GAMERTAG
            real_name = ""
            account_tier = "Gold"
            try:
                note_api_call()
                account_res = await client.get("https://xbl.io/api/v2/account", headers=headers, timeout=10)
                if account_res.status_code == 200:
                    acc = account_res.json() or {}
                    acc_content = acc.get("content") or acc
                    pus = acc_content.get("profileUsers") or []
                    if pus:
                        pu = pus[0]
                        settings_list = pu.get("settings") or []
                        settings = {s.get("id"): s.get("value") for s in settings_list if isinstance(s, dict)}
                        gamertag = settings.get("Gamertag") or settings.get("ModernGamertag") or gamertag
                        gamerpic = settings.get("GameDisplayPicRaw", "")
                        try:
                            gamerscore = int(settings.get("Gamerscore", 0))
                        except:
                            gamerscore = 0
                        try:
                            tenure = int(settings.get("TenureLevel", 0))
                        except:
                            tenure = 0
                        # Real name (may not be present on all accounts / privacy settings)
                        real_name = (
                            settings.get("RealName")
                            or settings.get("FirstName")
                            or ""
                        )
                        if not real_name:
                            fn = settings.get("FirstName", "") or ""
                            ln = settings.get("LastName", "") or ""
                            real_name = (fn + " " + ln).strip()
                        # Account tier (often Gold / Game Pass etc.)
                        account_tier = settings.get("AccountTier") or settings.get("Tier") or "Gold"
            except Exception as e:
                print(f"Xbox account fetch error: {e}")

            # Log meaningful game changes (only when game actually changes and is valid)
            if game and game != "—":
                try:
                    log = persistence.load_xbox_log()
                    last_entry = log[0] if log else None
                    if not last_entry or last_entry.get("game") != game:
                        entry = {
                            "game": game,
                            "timestamp": datetime.now().isoformat(),
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
                "gamertag": gamertag,
                "gamerpic": gamerpic,
                "gamerscore": gamerscore,
                "tenure": tenure,
                "real_name": real_name,
                "account_tier": account_tier,
                "xuid": xuid or XBL_XUID,
                "last_updated": fetch_time
            }
            state._last_xbox_fetch_time = fetch_time
            return state.last_xbox_data

        except Exception as e:
            print(f"Xbox API error: {e}")
            return state.last_xbox_data


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
            presence_state = data.get("state", "Offline") or "Offline"
            game = ""
            device_type = ""
            for device in (data.get("devices") or []):
                if not isinstance(device, dict):
                    continue
                for title in (device.get("titles") or []):
                    if isinstance(title, dict) and title.get("placement") == "Full":
                        game = title.get("name") or ""
                        device_type = device.get("type", "") or ""
                        break
                if game:
                    break
            return (game if is_real_game(game) else "", device_type, presence_state)
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
    now = datetime.now()
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
            "closed": False, "device": device, "samples": 1,
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


def is_real_game(name: str) -> bool:
    if not name:
        return False
    return name.strip().lower() not in NON_GAME_TITLES


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


def sessions_for_display(sessions: list, limit: int = 40) -> list:
    """Format real play sessions into a readable timeline (newest first):
    game, when it started, how long, device, and whether it's live right now."""
    out = []
    for s in sessions or []:
        if not is_real_game(s.get("game", "")):
            continue
        try:
            start = datetime.fromisoformat(s["start"])
            end = datetime.fromisoformat(s["end"])
        except Exception:
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
            "started": start.strftime("%b %-d • %-I:%M %p"),
            "length": length,
            "device": _pretty_device(s.get("device", "")),
            "live": live,
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


