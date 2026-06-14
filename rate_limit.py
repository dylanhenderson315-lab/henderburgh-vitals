"""Rate limiting for public deployments."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Dict, List

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from config import PUBLIC_MODE


class SimpleRateLimiter:
    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self.requests: Dict[str, List[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window_start = now - self.window
        self.requests[key] = [t for t in self.requests[key] if t > window_start]
        if len(self.requests[key]) >= self.max_requests:
            return False
        self.requests[key].append(now)
        return True


rate_limiter = SimpleRateLimiter(
    max_requests=40 if PUBLIC_MODE else 120,
    window_seconds=60,
)

write_rate_limiter = SimpleRateLimiter(
    max_requests=10 if PUBLIC_MODE else 30,
    window_seconds=60,
)


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "") or ""
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply rate limits in public mode to all routes; stricter on writes."""

    async def dispatch(self, request: Request, call_next):
        if not PUBLIC_MODE:
            return await call_next(request)

        ip = client_ip(request)
        is_write = request.method in ("POST", "PUT", "PATCH", "DELETE")
        limiter = write_rate_limiter if is_write else rate_limiter

        if not limiter.is_allowed(ip):
            if request.url.path.startswith("/api/"):
                return JSONResponse(
                    {"detail": "Too many requests. Please slow down."},
                    status_code=429,
                )
            return Response(
                content=(
                    "<div style='font-family:sans-serif;padding:40px;text-align:center;color:#666'>"
                    "Too many requests. Please slow down.</div>"
                ),
                status_code=429,
                media_type="text/html",
            )

        return await call_next(request)
