"""Xbox Live status via xbl.io."""

from __future__ import annotations

import time
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


