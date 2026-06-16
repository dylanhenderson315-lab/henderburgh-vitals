"""Application configuration from environment variables."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

OURA_TOKEN = os.getenv("OURA_TOKEN", "").strip()
OURA_DAYS = int(os.getenv("OURA_DAYS", "14"))
OURA_BASE_URL = "https://api.ouraring.com/v2"

PUBLIC_MODE = os.getenv("PUBLIC_MODE", "false").lower() in ("1", "true", "yes")
DISPLAY_NAME = (os.getenv("DISPLAY_NAME", "HENDERBURGH").strip() or "HENDERBURGH").upper()
SITE_NAME = (os.getenv("SITE_NAME", "HENDERBURGH").strip() or "HENDERBURGH").upper()
SITE_URL = os.getenv("SITE_URL", "https://henderburgh.com")

# Directory for persisted JSON (model, blog, etc.). On Railway, point this at a
# mounted volume (e.g. DATA_DIR=/data) so saved rooms/furniture survive redeploys.
# Defaults to the in-repo ./data for local development.
DATA_DIR = os.getenv("DATA_DIR", "data").strip() or "data"

DEFAULT_CACHE_TTL = 600 if PUBLIC_MODE else 180
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL))
HEARTRATE_CACHE_TTL = int(os.getenv("HEARTRATE_CACHE_TTL", "120" if PUBLIC_MODE else "30"))

PORT = int(os.getenv("PORT", 8000))
AUTO_REFRESH_SECONDS = int(os.getenv("AUTO_REFRESH_SECONDS", "300"))

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
SESSION_COOKIE_NAME = "admin_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600

XBL_PLACEHOLDER_KEYS = ("your_real_xbl_key_here", "your_openxbl_api_key_here")
XBL_PLACEHOLDER_GAMERTAGS = ("your_gamertag_here", "your_exact_gamertag_here")
XBL_API_KEY = os.getenv("XBL_API_KEY", "").strip()
XBL_GAMERTAG = os.getenv("XBL_GAMERTAG", "").strip()
XBL_XUID = os.getenv("XBL_XUID", "").strip()
XBOX_CACHE_TTL_SECONDS = 5 * 60

HA_URL = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "").strip()
HA_ENABLED = (not PUBLIC_MODE) and bool(HA_TOKEN) and bool(HA_URL)
HA_HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
} if HA_TOKEN else {}

OFFICE_LAMP = "light.office_lamp"
HA_BOOTSTRAP_CACHE_TTL = 45

VITALS_SNAPSHOT_TTL = CACHE_TTL_SECONDS

if XBL_API_KEY in XBL_PLACEHOLDER_KEYS or XBL_GAMERTAG in XBL_PLACEHOLDER_GAMERTAGS:
    print(
        "WARNING: Using placeholder XBL_API_KEY / XBL_GAMERTAG. "
        "Xbox status will be unavailable until you set real values in .env or Railway."
    )

if PUBLIC_MODE:
    if not OURA_TOKEN:
        raise RuntimeError(
            "PUBLIC_MODE is enabled but OURA_TOKEN is not set. "
            "Set OURA_TOKEN as an environment variable on your hosting platform."
        )
    print(f"Running in PUBLIC_MODE for {SITE_URL} — token required, aggressive caching enabled.")
else:
    if not OURA_TOKEN:
        print("OURA_TOKEN not set. Dashboard will show setup instructions.")
    if not HA_TOKEN:
        print("HA_TOKEN not set. Home Assistant features will be unavailable.")

if PUBLIC_MODE and HA_TOKEN:
    print("HA integration loaded but disabled because PUBLIC_MODE=true (controls hidden).")
