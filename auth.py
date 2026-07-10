"""Admin authentication via HttpOnly session cookie or Authorization header.

Sessions are persisted under DATA_DIR so Railway redeploys don't silently
log everyone out (which made Guest Mode feel "still locked" after deploys).
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import HTTPException, Request, Response

from config import ADMIN_TOKEN, DATA_DIR, SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS, SITE_URL

_admin_sessions: Dict[str, float] = {}
_sessions_loaded = False


def _sessions_path() -> Path:
    base = Path(DATA_DIR)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return base / "admin_sessions.json"


def _load_sessions() -> None:
    global _sessions_loaded, _admin_sessions
    if _sessions_loaded:
        return
    _sessions_loaded = True
    path = _sessions_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            now = time.time()
            _admin_sessions = {
                str(k): float(v)
                for k, v in data.items()
                if isinstance(v, (int, float)) and float(v) > now
            }
    except Exception as e:
        print(f"admin session load error: {e}")


def _save_sessions() -> None:
    path = _sessions_path()
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_admin_sessions), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        print(f"admin session save error: {e}")


def _purge_expired_sessions() -> None:
    _load_sessions()
    now = time.time()
    expired = [sid for sid, exp in _admin_sessions.items() if exp <= now]
    if not expired:
        return
    for sid in expired:
        _admin_sessions.pop(sid, None)
    _save_sessions()


def create_admin_session() -> str:
    _purge_expired_sessions()
    session_id = secrets.token_urlsafe(32)
    _admin_sessions[session_id] = time.time() + SESSION_MAX_AGE_SECONDS
    _save_sessions()
    return session_id


def invalidate_admin_session(session_id: Optional[str]) -> None:
    _load_sessions()
    if session_id and session_id in _admin_sessions:
        _admin_sessions.pop(session_id, None)
        _save_sessions()


def is_admin_authenticated(request: Request) -> bool:
    if not ADMIN_TOKEN:
        return False

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if hmac.compare_digest(token, ADMIN_TOKEN):
            return True

    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        _purge_expired_sessions()
        exp = _admin_sessions.get(session_id)
        if exp and time.time() < exp:
            return True

    return False


def require_admin(request: Request) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Admin token not configured")
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=403, detail="Admin authentication required")


def _cookie_secure() -> bool:
    """Secure flag for the admin session cookie.

    On the real HTTPS site we want Secure cookies. TestClient / local http must
    not use Secure or the browser (and TestClient) will drop the cookie.
    Override with COOKIE_SECURE=0/1 if needed.
    """
    import os
    flag = os.getenv("COOKIE_SECURE", "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    if flag in ("1", "true", "yes"):
        return True
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return str(SITE_URL or "").lower().startswith("https")


def set_admin_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
        secure=_cookie_secure(),
        path="/",
    )


def clear_admin_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
