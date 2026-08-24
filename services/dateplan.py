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
import uuid
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

# --- Storage paths ---------------------------------------------------
# All persistent state lives under LOG_PATH.parent so a single Railway
# Volume covers pick log, replies, uploaded photos, and the context
# notepad. Older deploys used /tmp; the DATE_LOG_PATH env var swings
# everything to /data/... on the Volume.
_STATE_DIR = LOG_PATH.parent
PHOTO_DIR = _STATE_DIR / "photos"
CONTEXT_PATH = _STATE_DIR / "context.jsonl"
GIFTS_PATH = _STATE_DIR / "gifts.jsonl"
WEEKLY_PATH = _STATE_DIR / "weekly.jsonl"

_lock = threading.Lock()
_context_lock = threading.Lock()


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


def real_pick_count():
    """Total real (non-test) picks in the log -- drives the "date night #N"
    badge on the confirmation screen. Excludes replies (which are their
    own row kind) and rehearsals."""
    n = 0
    if not LOG_PATH.exists():
        return 0
    with LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "reply":
                continue
            if r.get("test"):
                continue
            # A "real pick" row is one that at least named a movie or dinner
            # -- guards against a stray empty POST inflating the count.
            if r.get("movie") or r.get("dinner"):
                n += 1
    return n


# Milestones that get extra confetti + a bigger badge on the confirm screen.
# Kept intentionally sparse -- every date is special, but pretending every
# single one is a milestone means none of them are.
MILESTONES = {1, 5, 10, 25, 52, 100}


def photo(filename, viewer=None):
    """Register that a photo was uploaded and stored to PHOTO_DIR/<filename>.
    The file itself was saved by the endpoint; this just puts a row in the
    log so the inbox can pin it under the nearest preceding pick, exactly
    like replies. Kept as its own row kind so it never inflates the pick
    counter."""
    v = _clean_viewer(viewer)
    row = {
        "ts": time.time(),
        "kind": "photo",
        "filename": filename,
        "viewer": v,
        "test": v is not None,
    }
    with _lock:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            row["log_error"] = str(e)
    return row


# --- Context notepad ---------------------------------------------------
# He drops notes throughout the week ("she walked the beach Wednesday",
# "she wants to try that new coffee place", "work has been rough") so
# that when it's time to plan the next date, everything he was paying
# attention to is right there in one place. Rows are append-only jsonl,
# same discipline as the pick log; nothing is ever mutated in place.

# A curated tag palette. Kept small so he actually uses them -- freeform
# tag entry always drifts into typos and near-duplicates over time.
CONTEXT_TAGS = [
    "she-said",     # something she wanted / mentioned wanting
    "we-did",       # something we did together this week
    "win",          # thing that went well
    "rough",        # something hard she went through
    "beach",        # any beach-adjacent context
    "food",         # restaurants tried, meals cooked, cravings
    "coffee",       # coffee shops / coffee wants
    "books",        # bookstore / reading
    "outdoors",     # farmers market, gardens, hikes, park
    "cozy",         # in-the-house energy
    "work",         # her job context, useful for tone
    "plan",         # explicit "we should do X"
    "note",         # generic
]


def add_context(text, tags=None):
    """Append one note to the weekly context log. Tags outside the
    curated palette are silently dropped rather than stored -- they
    drift and become near-duplicates otherwise."""
    if not isinstance(text, str) or not text.strip():
        return None
    clean_tags = []
    if isinstance(tags, list):
        seen = set()
        for t in tags:
            if not isinstance(t, str):
                continue
            t = t.strip().lower()
            if t in CONTEXT_TAGS and t not in seen:
                clean_tags.append(t)
                seen.add(t)
    row = {
        "ts": time.time(),
        "text": text.strip()[:2000],
        "tags": clean_tags,
    }
    with _context_lock:
        try:
            CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with CONTEXT_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            row["log_error"] = str(e)
    return row


def context(days=14, limit=200):
    """Recent context notes, newest last. Defaults cover about two weeks
    (enough for a planning session) without dumping the full history."""
    if not CONTEXT_PATH.exists():
        return []
    cutoff = time.time() - (days * 86400)
    out = []
    with CONTEXT_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ts", 0) >= cutoff:
                out.append(r)
    return out[-limit:]


def request_night(vibe=None, when=None, note=None, viewer=None):
    """She sends him a request card -- the mirror of the invite. Vibes
    are curated (like MOVIES/DINNERS ids) so the log stays typed and
    the inbox can render them cleanly."""
    v = _clean_viewer(viewer)
    vibe = vibe if vibe in REQUEST_VIBES else None
    when = when if when in REQUEST_WHENS else None
    row = {
        "ts": time.time(),
        "kind": "request",
        "vibe": vibe,
        "vibe_label": REQUEST_VIBES.get(vibe),
        "when": when,
        "when_label": REQUEST_WHENS.get(when),
        "note": (note.strip()[:1000] if isinstance(note, str) else None) or None,
        "viewer": v,
        "test": v is not None,
    }
    with _lock:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            row["log_error"] = str(e)
    return row


# Curated vibe palette for requests. Kept small on purpose -- lets the
# inbox render clean typed chips instead of freeform strings that drift.
REQUEST_VIBES = {
    "coffee":    "a coffee shop morning",
    "cozy":      "a cozy night in",
    "adventure": "something new · adventure",
    "dinner":    "a real dinner out",
    "beach":     "beach time",
    "outdoors":  "get outside · farmers market, gardens, walking",
    "surprise":  "surprise me · you pick everything",
}
REQUEST_WHENS = {
    "tonight":  "tonight",
    "tomorrow": "tomorrow",
    "week":     "sometime this week",
    "weekend":  "this weekend",
    "soon":     "soon, when you can",
}


def add_gift(title, body, tags=None):
    """He drops a "just because" -- a small note, a photo caption, an
    inside joke, whatever. Lives in a separate gifts.jsonl so it never
    tangles with picks/replies and so unread state has a natural home
    (unread = ts > last_opened_ts, stored below)."""
    if not isinstance(title, str) or not title.strip():
        title = "just because"
    if not isinstance(body, str) or not body.strip():
        return None
    row = {
        "ts": time.time(),
        "id": uuid.uuid4().hex[:12],
        "title": title.strip()[:120],
        "body": body.strip()[:4000],
        "tags": tags if isinstance(tags, list) else [],
        "opened_ts": None,
    }
    with _lock:
        try:
            GIFTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with GIFTS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            row["log_error"] = str(e)
    return row


def gifts(limit=100):
    """Every gift he's ever left, newest last. Missing file = empty."""
    if not GIFTS_PATH.exists():
        return []
    out = []
    with GIFTS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out[-limit:]


def mark_gift_opened(gift_id):
    """Persist the opened_ts by rewriting the file. Tiny file, one row
    per gift -- rewrite is cheaper than any real index and it lets us
    keep the single-file jsonl discipline everywhere else."""
    if not GIFTS_PATH.exists():
        return False
    rows = gifts(limit=10000)
    changed = False
    for r in rows:
        if r.get("id") == gift_id and not r.get("opened_ts"):
            r["opened_ts"] = time.time()
            changed = True
    if not changed:
        return False
    with _lock:
        tmp = GIFTS_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        tmp.replace(GIFTS_PATH)
    return True


def set_edition(star_title=None, star_body=None, star_emoji=None,
                star_tag=None, letter_override=None):
    """He publishes THIS WEEK's invite overrides from the planner. Simple
    for now -- one edition row per publish, latest one wins. jsonl kept
    append-only so history is preserved if he wants to look back at
    which weeks had which star cards."""
    row = {
        "ts": time.time(),
        "star_title": (star_title or "").strip()[:120] or None,
        "star_body": (star_body or "").strip()[:600] or None,
        "star_emoji": (star_emoji or "").strip()[:8] or None,
        "star_tag": (star_tag or "").strip()[:40] or None,
        "letter_override": (letter_override or "").strip()[:400] or None,
    }
    # No-op if every field is empty -- refuses to save an edition that
    # would change nothing on the invite.
    if not any([row["star_title"], row["star_body"], row["letter_override"]]):
        return None
    with _lock:
        try:
            WEEKLY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with WEEKLY_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            row["log_error"] = str(e)
    return row


def current_edition():
    """The latest published edition, or None. If nothing published, the
    invite falls back to its hardcoded defaults."""
    if not WEEKLY_PATH.exists():
        return None
    latest = None
    with WEEKLY_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                latest = json.loads(line)
            except ValueError:
                continue
    return latest


def rate(rating, viewer=None):
    """One-tap emotional read on last night. Curated palette so the
    inbox can render typed chips instead of freeform strings; anything
    else silently drops rather than store a garbage value."""
    if rating not in RATINGS:
        return None
    v = _clean_viewer(viewer)
    row = {
        "ts": time.time(),
        "kind": "rating",
        "rating": rating,
        "rating_label": RATINGS[rating],
        "viewer": v,
        "test": v is not None,
    }
    with _lock:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            row["log_error"] = str(e)
    return row


# Emojis and their meaning. Small palette on purpose -- forced choice
# gives a signal, freeform ratings become "it was nice" every time.
RATINGS = {
    "fire":    "🔥 top-tier · do this again",
    "warm":    "🙌 loved it",
    "sparkle": "💫 something magic happened",
    "calm":    "🌊 exactly what I needed",
    "quiet":   "💤 low-key, low-energy",
}


def reply(note, viewer=None):
    """She (or a friend, in rehearsal) writes a message back after the
    reservation is locked in. Stored as its own row kind so the inbox can
    group it under the nearest preceding pick without confusing the
    real_pick_count."""
    if not isinstance(note, str) or not note.strip():
        return None
    v = _clean_viewer(viewer)
    row = {
        "ts": time.time(),
        "kind": "reply",
        "note": note.strip()[:2000],
        "viewer": v,
        "test": v is not None,
    }
    with _lock:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            row["log_error"] = str(e)
    return row
