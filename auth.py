"""Admin authentication via HttpOnly session cookie or Authorization header."""

from __future__ import annotations

import hmac
import secrets
import time
from typing import Dict, Optional

from fastapi import HTTPException, Request, Response

from config import ADMIN_TOKEN, SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS

_admin_sessions: Dict[str, float] = {}


def _purge_expired_sessions() -> None:
    now = time.time()
    expired = [sid for sid, exp in _admin_sessions.items() if exp <= now]
    for sid in expired:
        _admin_sessions.pop(sid, None)


def create_admin_session() -> str:
    _purge_expired_sessions()
    session_id = secrets.token_urlsafe(32)
    _admin_sessions[session_id] = time.time() + SESSION_MAX_AGE_SECONDS
    return session_id


def invalidate_admin_session(session_id: Optional[str]) -> None:
    if session_id:
        _admin_sessions.pop(session_id, None)


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


def set_admin_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
        secure=False,
    )


def clear_admin_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME)
