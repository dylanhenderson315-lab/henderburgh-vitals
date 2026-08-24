#!/usr/bin/env python3
"""Local companion server for Henderburgh's personal Date pages.

Runs on the home LAN and serves the planner + inbox at bare paths
(no admin token in URL). Every API call it makes to henderburgh.com
gets the admin token attached server-side, so the browser never sees
it. Nobody on the public internet can hit these pages because this
process only binds inside the house.

Boot: `python3 date_local.py`
Reachable at:
  http://Dylans-Mac-mini.local:8899/plan    (his weekly notepad)
  http://Dylans-Mac-mini.local:8899/inbox   (past dates + replies + photos)
Or by LAN IP: http://192.168.40.203:8899/plan

Env overrides:
  ADMIN_TOKEN   -- the token this server tacks onto proxied API calls
  HENDERBURGH_REMOTE -- override the remote origin (default henderburgh.com)
  PORT          -- default 8899
  HOST          -- default 0.0.0.0 (bind on all interfaces)
"""

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REMOTE = os.getenv("HENDERBURGH_REMOTE", "https://henderburgh.com").rstrip("/")
TOKEN = os.getenv("ADMIN_TOKEN", "2824")
PORT = int(os.getenv("PORT", "8899"))
HOST = os.getenv("HOST", "0.0.0.0")

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, ctype, body):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # LAN-only, but let the hub page probe /ping cross-origin
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _proxy(self, method, remote_path, body=None, ctype=None):
        """Blocking passthrough to Railway with the admin token already
        applied where needed. Read timeout is generous -- these are
        JSON calls, not big blobs, but photo proxying can chunk."""
        try:
            # Real User-Agent: Cloudflare's default bot rules 403 the
            # stdlib "Python-urllib/x" UA, which was silently returning
            # empty payloads to the browser.
            headers = {
                "Accept": "*/*",
                "User-Agent": "Henderburgh-LAN-Companion/1.0 (Mozilla/5.0)",
            }
            if body is not None and ctype:
                headers["Content-Type"] = ctype
            req = urllib.request.Request(
                REMOTE + remote_path, data=body, method=method, headers=headers
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                data = r.read()
                self._send(r.status,
                           r.headers.get("Content-Type", "application/octet-stream"),
                           data)
        except urllib.error.HTTPError as e:
            self._send(e.code, "application/json",
                       json.dumps({"ok": False, "code": e.code}).encode())
        except Exception as e:
            self._send(502, "application/json",
                       json.dumps({"ok": False, "error": str(e)}).encode())

    # ---- routing ----

    def do_OPTIONS(self):
        self._send(204, "text/plain", b"")

    def do_GET(self):
        p = urlparse(self.path).path
        # health / discovery
        if p == "/ping":
            return self._send(200, "application/json",
                              json.dumps({"ok": True, "where": "local",
                                          "remote": REMOTE}).encode())
        # planner + inbox served straight off disk
        if p in ("/", "/plan", "/plan/"):
            return self._send(200, "text/html; charset=utf-8",
                              (TEMPLATES / "plan.html").read_bytes())
        if p in ("/inbox", "/inbox/"):
            return self._send(200, "text/html; charset=utf-8",
                              (TEMPLATES / "date_inbox.html").read_bytes())
        # proxied reads
        if p == "/api/context":
            return self._proxy("GET", f"/api/context/{TOKEN}")
        if p == "/api/inbox":
            return self._proxy("GET", f"/api/date/inbox-json/{TOKEN}")
        if p == "/api/gifts":
            return self._proxy("GET", f"/api/gifts/{TOKEN}")
        if p == "/api/edition":
            return self._proxy("GET", "/api/date/edition/lawdog")
        if p == "/api/suggest-edition":
            return self._proxy("GET", f"/api/date/suggest-edition/{TOKEN}")
        # photos + audio: pass through unchanged
        if (p.startswith("/photos/") or p.startswith("/audio/")) and ".." not in p and "\\" not in p:
            return self._proxy("GET", p)
        self._send(404, "text/plain", b"nope")

    def do_POST(self):
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        # Inject the admin token into the body server-side. Browser posts
        # {text, tags}; we add {token} before forwarding.
        if p in ("/api/context", "/api/gifts", "/api/edition"):
            try:
                obj = json.loads(body or b"{}")
            except ValueError:
                return self._send(400, "application/json",
                                  b'{"ok":false,"error":"bad body"}')
            obj["token"] = TOKEN
            # /api/edition on Railway lives at /api/date/edition
            remote = "/api/date/edition" if p == "/api/edition" else p
            return self._proxy("POST", remote,
                               json.dumps(obj).encode(), "application/json")
        self._send(404, "text/plain", b"nope")

    def log_message(self, fmt, *args):
        # Quiet; use `PYTHONUNBUFFERED=1` if you want to add real logging.
        return


if __name__ == "__main__":
    if not TOKEN:
        print("! ADMIN_TOKEN is empty -- proxied API calls will 401.",
              file=sys.stderr)
    print(f"→ Henderburgh local companion")
    print(f"  planner : http://Dylans-Mac-mini.local:{PORT}/plan")
    print(f"  inbox   : http://Dylans-Mac-mini.local:{PORT}/inbox")
    print(f"  or IP   : http://192.168.40.203:{PORT}/plan   (LAN only)")
    print(f"  proxying to  {REMOTE}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
