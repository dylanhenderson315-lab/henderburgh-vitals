"""Nicole's date invitation, mirrored from the arcade repo so it can serve
from henderburgh.com (Railway) instead of only from the LAN box.

Same shape as wled-m1-arcade/dateplan.py -- same MOVIES / DINNERS / SECRET
so the copy on the page keeps working unchanged. Only difference: the
storage path is /tmp/date_log.jsonl on Railway (writable, ephemeral) with
a fallback to a repo-local storage/ dir for local testing.

Two things here are deliberate (also copied from the arcade version):

  * SECRET on the URL. henderburgh.com is publicly reachable. A love
    letter plus a phone-notification trigger sitting on a guessable path
    is not something to find out about later, so the page is behind a
    word only she has. Obscurity, not auth -- appropriate for the stakes.

  * Picks are APPENDED, never overwritten. She may open twice, resubmit
    with a different dinner tier, or a friend may test-tap several times;
    every one of those is a real event worth keeping.
"""

import json
import os
import threading
import time
from pathlib import Path

# Railway's filesystem is ephemeral -- fine for a one-night notepad, but
# use a persistent location if one is mounted via env, and fall back to
# /tmp otherwise so writes never fail on the deploy host.
LOG_PATH = Path(os.getenv("DATE_LOG_PATH", "/tmp/date_log.jsonl"))

SECRET = "lawdog"

MOVIES = {
    "comfort":  "A COMFORT REWATCH",
    "dumb":     "SOMETHING DUMB AND FUNNY",
    "cry":      "SOMETHING THAT WILL MAKE ME CRY",
    "boom":     "SOMETHING WITH EXPLOSIONS",
    "you":      "SHE TRUSTS YOU TO PICK THE MOVIE",
}

DINNERS = {
    # Tonight's actual front-runner -- she already went to the store and
    # brought home crab legs + shrimp. Kept at the top of the dinner list.
    "cooking":  "CRAB LEGS + SHRIMP, SHE'S COOKING",
    "gas":      "GAS STATION LEGAL MINIMUM",
    "takeout":  "TAKEOUT IN SWEATS",
    "nice":     "A NICE SIT DOWN",
    "boujie":   "FULL BOUJIE, REAL NAPKINS",
    "surprise": "SHE TRUSTS YOU TO PLAN DINNER, READY BY 7",
}

# Bonus decision -- extends the evening into a proper weekend gesture.
# She loves coffee dates and cute bookstores; the options here are that,
# plus a trust-me and a totally-honest sleep-in.
MORNINGS = {
    "coffee":   "BRAND NEW COFFEE SHOP",
    "bookworm": "BOOKWORM AND VINE, BOOKS AND WINE",
    "books":    "LITCHFIELD BOOKS ALL MORNING",
    "gardens":  "BROOKGREEN GARDENS, SLOW",
    "perfect":  "COFFEE + BOOKSTORE, NO CLOCK",
    "surprise": "SHE TRUSTS YOU TO PLAN THE MORNING",
    "skip":     "SLEEP IN, ZERO PLANS",
}

HELI_LABEL = "THE 30 DOLLAR HELICOPTER RIDE"

_lock = threading.Lock()


def label_movie(mid):
    return MOVIES.get(mid)


def label_dinner(did):
    return DINNERS.get(did)


def label_morning(mid):
    return MORNINGS.get(mid)


def _clean_viewer(v):
    if not isinstance(v, str):
        return None
    keep = "".join(c for c in v if c.isalnum() or c in " _.-")
    keep = keep.strip()[:40]
    return keep or None


def summary(movie=None, dinner=None, morning=None, heli=False):
    parts = []
    m = label_movie(movie) if movie else None
    d = label_dinner(dinner) if dinner else None
    mo = label_morning(morning) if morning else None
    if m:
        parts.append("MOVIE: " + m)
    if d:
        parts.append("DINNER: " + d)
    if mo:
        parts.append("AM: " + mo)
    if heli:
        parts.append("+ " + HELI_LABEL)
    return " / ".join(parts) if parts else "SHE OPENED IT"


def record(movie=None, dinner=None, morning=None, heli=False, note=None, viewer=None):
    v = _clean_viewer(viewer)
    m = label_movie(movie) if movie else None
    d = label_dinner(dinner) if dinner else None
    mo = label_morning(morning) if morning else None
    row = {
        "ts": time.time(),
        "movie": movie if m else None,
        "movie_label": m,
        "dinner": dinner if d else None,
        "dinner_label": d,
        "morning": morning if mo else None,
        "morning_label": mo,
        "heli": bool(heli),
        "viewer": v,
        "test": v is not None,
    }
    if isinstance(note, str) and note.strip():
        row["note"] = note.strip()[:500]
    banner = summary(movie, dinner, morning, heli)
    if v:
        banner = "TEST [" + v.upper() + "] " + banner
    row["banner"] = banner
    with _lock:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            row["log_error"] = str(e)
    return row


def picks(limit=50):
    if not LOG_PATH.exists():
        return []
    out = []
    with LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out[-limit:]
