"""Push notifications via ntfy.sh -- free, no signup, no auth.

Config is one env var:

  NTFY_TOPIC=<random-word-nobody-else-would-guess>

He installs the ntfy iOS app, subscribes to that topic, and every
notify(...) call in this module wakes his phone. If NTFY_TOPIC is not
set the whole module silently no-ops -- safe to leave live in code
even when he hasn't set up his phone yet.

Kept deliberately tiny (stdlib only, one function). Any date event
that calls notify() is a fire-and-forget from a BackgroundTask; a
failed push must never break the real request path.
"""

import os
import urllib.request
import urllib.error


TOPIC = os.getenv("NTFY_TOPIC", "").strip()
BASE = os.getenv("NTFY_BASE", "https://ntfy.sh").rstrip("/")


def enabled():
    return bool(TOPIC)


def notify(title, message, priority="default", tags=None, click=None):
    """Fire a push. Silent no-op if NTFY_TOPIC isn't set.

    - title: bold header on the notification
    - message: body text
    - priority: min | low | default | high | urgent (ntfy's own scale)
    - tags: list[str] -- ntfy renders known ones as emoji (e.g. "heart",
      "cocktail", "coffee", "camera_flash")
    - click: URL the notification opens when tapped (e.g. link to
      /date/inbox on his LAN)
    """
    if not TOPIC:
        return False
    headers = {
        "Title": _hdr(title),
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(t for t in tags if isinstance(t, str))
    if click:
        headers["Click"] = click
    try:
        req = urllib.request.Request(
            f"{BASE}/{TOPIC}",
            data=(message or "").encode("utf-8"),
            method="POST",
            headers=headers,
        )
        urllib.request.urlopen(req, timeout=4).read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _hdr(s):
    """ntfy headers must be latin-1 and single-line. Strip newlines and
    down-convert anything exotic so an emoji in a title doesn't 500 the
    request; the emoji is fine in the message body."""
    s = (s or "").replace("\r", " ").replace("\n", " ")
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        return s.encode("ascii", "ignore").decode("ascii") or "Henderburgh"
