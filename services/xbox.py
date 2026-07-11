"""Xbox Live status via xbl.io."""

from __future__ import annotations

import hashlib
import time
from collections import Counter
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


def compute_gaming_insights(raw_log: list, current_game: str = "") -> dict:
    """Turn the raw presence log into truthful, quirky, statistics-based narration —
    the vitals insight engine, aimed at gaming. Only real games count."""
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

    total = len(entries)
    out = {"has_data": total >= 3, "sessions": total, "insights": [], "top_games": [], "stats": {}}
    if total == 0:
        return out

    counts = Counter(e["game"] for e in entries)
    distinct = len(counts)
    ranked = counts.most_common()
    fav, fav_n = ranked[0]

    top_games = [{"name": g, "count": c, "pct": round(100 * c / total)} for g, c in ranked[:5]]
    out["top_games"] = top_games

    dated = [e for e in entries if e["dt"]]
    night_n = sum(1 for e in dated if (e["dt"].hour >= 21 or e["dt"].hour < 4))
    weekend_n = sum(1 for e in dated if e["dt"].weekday() >= 5)
    weekday_n = len(dated) - weekend_n

    # Longest streak of consecutive calendar days with a real-game session.
    days = sorted({e["dt"].date() for e in dated})
    longest = cur = 1 if days else 0
    for i in range(1, len(days)):
        if (days[i] - days[i - 1]).days == 1:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 1

    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    busiest_day = None
    if dated:
        day_counts = Counter(e["dt"].weekday() for e in dated)
        busiest_day = weekday_names[day_counts.most_common(1)[0][0]]

    span_days = (days[-1] - days[0]).days + 1 if len(days) >= 2 else 0

    out["stats"] = {
        "distinct_games": distinct,
        "favorite": fav,
        "favorite_pct": round(100 * fav_n / total),
        "night_pct": round(100 * night_n / len(dated)) if dated else 0,
        "longest_streak": longest,
        "busiest_day": busiest_day,
        "span_days": span_days,
    }

    ins = out["insights"]
    # Favorite / obsession
    recent10 = [e["game"] for e in entries[:10]]
    recent_fav = recent10.count(fav)
    if recent_fav >= 6:
        ins.append(f"**{fav}** is your obsession right now — {recent_fav} of your last {len(recent10)} sessions.")
    elif out["stats"]["favorite_pct"] >= 40:
        ins.append(f"**{fav}** is your main game — {out['stats']['favorite_pct']}% of everything you've launched.")
    else:
        ins.append(f"You spread your time around — **{distinct} different games**, led by {fav}.")

    # Night owl vs daytime
    if dated:
        np = out["stats"]["night_pct"]
        if np >= 55:
            ins.append(f"Certified night owl — **{np}%** of your sessions start after 9pm.")
        elif np <= 20:
            ins.append(f"Daytime gamer — only {np}% of your sessions are late-night.")

    # Streak
    if longest >= 3:
        ins.append(f"Your longest run was **{longest} days straight** with a controller in hand.")

    # Weekend skew
    if weekday_n > 0 and weekend_n > 0:
        # normalize: 2 weekend days vs 5 weekday days
        we_rate = weekend_n / 2
        wd_rate = weekday_n / 5
        if we_rate >= wd_rate * 1.8:
            ins.append(f"Weekends are your arena — you game **{we_rate / wd_rate:.1f}× more** per day than on weekdays.")

    # Busiest day + breadth
    if busiest_day and total >= 8:
        ins.append(f"**{busiest_day}** is your most-played day of the week.")
    if span_days >= 14:
        ins.append(f"{total} game sessions logged across the last **{span_days} days**.")

    # Current game context (truthful, only if actually playing something)
    cg = _clean_title(current_game)
    if cg and is_real_game(current_game) and cg != fav and counts.get(cg):
        ins.append(f"Right now: **{cg}** — you've been back to it {counts[cg]}× recently.")

    return out


