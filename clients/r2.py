"""Dependency-free Cloudflare R2 (S3-compatible) presigner.

Why this exists: henderburgh.com is behind Cloudflare, whose free/pro plans cap
request bodies at 100 MB — so large phone/Xbox clips can never reach the app
through the site. The fix is to let the phone upload the file STRAIGHT to R2 with
a short-lived presigned PUT URL (R2's own endpoint, not proxied by Cloudflare's
orange cloud), then register the resulting public URL as a normal clip link.

We sign with AWS Signature V4 by hand (hmac/hashlib only) to avoid pulling the
heavy boto3/botocore into the image just to build one URL. Path-style addressing
against the account endpoint, region 'auto', service 's3', UNSIGNED-PAYLOAD.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote

from config import (
    R2_ACCESS_KEY_ID,
    R2_ACCOUNT_ID,
    R2_BUCKET,
    R2_SECRET_ACCESS_KEY,
    R2_UPLOAD_ENABLED,
)

_REGION = "auto"
_SERVICE = "s3"

# Extension -> Content-Type so the stored object plays inline in the browser.
_CONTENT_TYPES = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".m4v": "video/x-m4v", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
}


def enabled() -> bool:
    return R2_UPLOAD_ENABLED


def content_type_for(ext: str) -> str:
    return _CONTENT_TYPES.get((ext or "").lower(), "application/octet-stream")


def _endpoint() -> str:
    return f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"


def _host() -> str:
    return f"{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(datestamp: str) -> bytes:
    k_date = _sign(("AWS4" + R2_SECRET_ACCESS_KEY).encode("utf-8"), datestamp)
    k_region = _sign(k_date, _REGION)
    k_service = _sign(k_region, _SERVICE)
    return _sign(k_service, "aws4_request")


def presign(method: str, key: str, content_type: str | None = None, expires: int = 3600) -> str:
    """Return a presigned URL for `method` on object `key` in the bucket.

    When `content_type` is given it is a SIGNED header, so the client MUST send
    exactly that Content-Type on the PUT (guarantees the object stores with the
    right type for inline playback and that the signature matches)."""
    now = datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    # Path-style: /<bucket>/<key>. Keep '/' and unreserved chars unescaped.
    canonical_uri = "/" + R2_BUCKET + "/" + quote(key, safe="/~")
    cred_scope = f"{datestamp}/{_REGION}/{_SERVICE}/aws4_request"

    if content_type:
        signed_headers = "content-type;host"
        canonical_headers = f"content-type:{content_type}\nhost:{_host()}\n"
    else:
        signed_headers = "host"
        canonical_headers = f"host:{_host()}\n"

    qp = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{R2_ACCESS_KEY_ID}/{cred_scope}",
        "X-Amz-Date": amzdate,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": signed_headers,
    }
    canonical_qs = "&".join(
        f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(qp.items())
    )

    canonical_request = "\n".join([
        method.upper(), canonical_uri, canonical_qs,
        canonical_headers, signed_headers, "UNSIGNED-PAYLOAD",
    ])
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amzdate, cred_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(datestamp), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return f"{_endpoint()}{canonical_uri}?{canonical_qs}&X-Amz-Signature={signature}"


def presign_put(key: str, content_type: str, expires: int = 3600) -> str:
    return presign("PUT", key, content_type=content_type, expires=expires)


def presign_delete(key: str, expires: int = 3600) -> str:
    return presign("DELETE", key, content_type=None, expires=expires)
