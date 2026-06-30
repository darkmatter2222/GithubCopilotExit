"""
Standalone HTTP server for the LLM Proxy Dashboard.

Serves index.html and proxies API requests to an upstream LLM proxy backend.

Architecture for remote deployment behind nginx ingress:
  Browser → nginx(/copilot/) → this server(serve.py) → upstream DGX Spark(:8001)

The HTML always injects PROXY_URL="" so the browser JS pFetch() calls use
relative paths (/stats, /v1/models, etc.). Those requests hit THIS server,
which then proxies them to the actual upstream backend (PROXY_BACKEND env var).

When deployed alongside the proxy on DGX Spark itself, leave PROXY_BACKEND empty
and set an injected PROXY_URL so browser calls go directly.

Authentication:
  DASHBOARD_USERNAME / DASHBOARD_PASSWORD — required username/password for login.
    When set, all routes except /login and /healthcheck are protected.
  PROXY_API_KEY — API key forwarded to the upstream proxy for API requests.
  ADMIN_USERNAME / ADMIN_PASSWORD — credentials forwarded to proxy /api/admin/* endpoints.
    Defaults to DASHBOARD_USERNAME / DASHBOARD_PASSWORD if not set.

Environment variables:
  PROXY_BACKEND / PROXY_URL / DASHBOARD_PROXY_URL — upstream URL for server-side
    proxying of API requests (e.g. "http://dgxspark:8001")
  HTML_PROXY_URL                                   — value injected into HTML for
    browser pFetch() calls. Default is "" (empty = same-origin). Set this to the
    direct proxy URL when dashboard runs on the same host as the proxy.
  PROXY_PATH_PREFIX                                — path prefix injected into HTML
    when behind nginx reverse-proxy (e.g. "/copilot" so browser fetches hit /copilot/stats)
  PROXY_PORT / DASHBOARD_PORT                      — listen port (default: 3000)

NOTE: For nginx ingress deployment, PROXY_BACKEND points to upstream AND
HTML_PROXY_URL stays empty so browser calls hit serve.py which proxies them.
"""

import base64
import http.server
import io
import json
import os
import secrets
import socketserver
import time
import urllib.request
import urllib.error
import urllib.parse
from urllib.parse import urljoin

PROXY_PORT = int(os.environ.get("PROXY_PORT", os.environ.get("DASHBOARD_PORT", "3000")))
# Server-side upstream target — ALWAYS set when deploying behind nginx ingress
PROXY_BACKEND = os.environ.get(
    "PROXY_BACKEND", os.environ.get("PROXY_URL", os.environ.get("DASHBOARD_PROXY_URL", "")))
# What to inject into HTML for browser pFetch() calls.
HTML_PROXY_URL = os.environ.get("HTML_PROXY_URL", "")

# Source HTML template
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "index.html")

# Paths that the dashboard JS pFetch() calls hit.
API_PREFIXES = ("/stats", "/v1/", "/api/")

# When deployed behind nginx at /copilot/, all browser fetch() paths need
# the prefix prepended.
PROXY_PATH_PREFIX = os.environ.get("PROXY_PATH_PREFIX", "")

# ── Authentication ──────────────────────────────────────────────────────────

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
AUTH_ENABLED = bool(DASHBOARD_USERNAME and DASHBOARD_PASSWORD)

# API key forwarded to the upstream proxy for all non-admin API requests.
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")

# Admin credentials forwarded when serving /api/admin/* requests to the proxy.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", DASHBOARD_USERNAME)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", DASHBOARD_PASSWORD)

SESSION_DURATION = 8 * 3600  # 8 hours

# In-memory session store: {token: expiry_timestamp}
_sessions: dict[str, float] = {}


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_DURATION
    return token


def _valid_session(token: str) -> bool:
    expiry = _sessions.get(token)
    if expiry and time.time() < expiry:
        return True
    _sessions.pop(token, None)
    return False


def _get_session_token(handler: http.server.BaseHTTPRequestHandler) -> str:
    for part in handler.headers.get("Cookie", "").split(";"):
        part = part.strip()
        if part.startswith("session="):
            return part[8:].strip()
    return ""


def _is_authenticated(handler: http.server.BaseHTTPRequestHandler) -> bool:
    if not AUTH_ENABLED:
        return True
    token = _get_session_token(handler)
    return bool(token) and _valid_session(token)


# ── Login page HTML ─────────────────────────────────────────────────────────

def _login_page(error: bool = False) -> bytes:
    error_html = '<div class="err">Invalid username or password.</div>' if error else ""
    # Use prefix-aware action so form POST works behind any nginx path prefix
    login_action = (PROXY_PATH_PREFIX or "") + "/login"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>DGX Spark · Login</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#080808;color:#e0e0e0;font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh}}
.card{{background:#0f0f0f;border:1px solid #2a2a2a;border-top:3px solid #76b900;
  border-radius:4px;padding:36px 40px;width:340px}}
.logo{{display:flex;align-items:center;gap:10px;margin-bottom:28px}}
.logo-mark{{width:28px;height:28px;background:#76b900;border-radius:3px;
  display:flex;align-items:center;justify-content:center;font-size:.6rem;
  font-weight:900;color:#000;letter-spacing:.04em}}
.logo-text{{font-size:.85rem;font-weight:700;letter-spacing:.04em}}
.logo-sub{{font-size:.62rem;color:#686868;letter-spacing:.03em;margin-top:1px}}
label{{display:block;font-size:.65rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.08em;color:#686868;margin-bottom:5px}}
input{{width:100%;background:#161616;border:1px solid #2a2a2a;border-radius:2px;
  color:#e0e0e0;font-size:.82rem;padding:8px 10px;margin-bottom:14px;outline:none}}
input:focus{{border-color:#76b900}}
button{{width:100%;background:#76b900;color:#000;border:none;border-radius:2px;
  font-size:.75rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
  padding:10px;cursor:pointer;margin-top:4px}}
button:hover{{background:#8ed000}}
.err{{background:rgba(224,64,64,.12);border:1px solid rgba(224,64,64,.2);
  color:#e04040;border-radius:2px;padding:8px 10px;font-size:.72rem;margin-bottom:14px}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-mark">DGX</div>
    <div><div class="logo-text">LLM Proxy</div><div class="logo-sub">DGX Spark Dashboard</div></div>
  </div>
  {error_html}
  <form method="POST" action="{login_action}">
    <label for="u">Username</label>
    <input id="u" name="username" type="text" autocomplete="username" required autofocus/>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required/>
    <button type="submit">Sign In</button>
  </form>
</div>
</body>
</html>"""
    return html.encode("utf-8")


# ── Upstream proxy helpers ──────────────────────────────────────────────────

def _build_auth_header(path: str) -> str:
    """Return the appropriate Authorization header value for the given path."""
    if path.startswith("/api/admin") and ADMIN_USERNAME and ADMIN_PASSWORD:
        encoded = base64.b64encode(f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}".encode()).decode()
        return f"Basic {encoded}"
    if PROXY_API_KEY:
        return f"Bearer {PROXY_API_KEY}"
    return ""


def _proxy_to_upstream(handler, path: str, method: str = "GET",
                       body: bytes = b"", content_type: str = ""):
    """Forward a request to the upstream PROXY_BACKEND and stream back the response."""
    if not PROXY_BACKEND:
        handler.send_response(502)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps({"error": "PROXY_BACKEND not configured"}).encode())
        return

    try:
        upstream_url = urljoin(PROXY_BACKEND, path)
        req_body = body if body else None
        req = urllib.request.Request(upstream_url, data=req_body, method=method)

        auth = _build_auth_header(path)
        if auth:
            req.add_header("Authorization", auth)

        if content_type:
            req.add_header("Content-Type", content_type)
        elif req_body:
            req.add_header("Content-Type", "application/json")

        resp = urllib.request.urlopen(req, timeout=30)
        resp_body = resp.read()
        handler.send_response(resp.status)
        for hdr_name, hdr_val in resp.getheaders():
            if hdr_name.lower() == "transfer-encoding":
                continue
            handler.send_header(hdr_name, hdr_val)
        handler.end_headers()
        handler.wfile.write(resp_body)
    except urllib.error.HTTPError as e:
        handler.send_response(e.code)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        try:
            handler.wfile.write(e.read())
        except Exception:
            handler.wfile.write(json.dumps({"error": f"upstream returned {e.code}"}).encode())
    except urllib.error.URLError as e:
        handler.send_response(502)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(
            json.dumps({"error": f"cannot reach proxy at {PROXY_BACKEND}",
                        "reason": str(e.reason)}).encode()
        )


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Serves index.html with authentication, healthcheck, and API proxy."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.dirname(__file__), **kwargs)

    def _serve_index(self):
        """Serve index.html with injected proxy configuration."""
        try:
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                html = f.read()
        except FileNotFoundError:
            html = "<h1>Dashboard not found</h1>"

        injection = (f'<script>window.__PROXY_URL="{HTML_PROXY_URL}";'
                     f'window.__BASE_PATH="{PROXY_PATH_PREFIX}";</script>')
        if "</head>" in html:
            html = html.replace("</head>", injection + "\n</head>", 1)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _serve_login(self, error: bool = False):
        body = _login_page(error)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _norm(self) -> str:
        """Normalize path: strip nginx prefix and query string."""
        path = self.path.split("?")[0]
        if PROXY_PATH_PREFIX and path.startswith(PROXY_PATH_PREFIX):
            path = path[len(PROXY_PATH_PREFIX):] or "/"
            if not path.startswith("/"):
                path = "/" + path
        return path

    def do_GET(self):
        p = self._norm()

        # Login page — public
        if p in ("/login", "/login/"):
            error = "error" in self.path
            self._serve_login(error)
            return

        # Logout
        if p == "/logout":
            token = _get_session_token(self)
            if token:
                _sessions.pop(token, None)
            self.send_response(302)
            self.send_header("Location", (PROXY_PATH_PREFIX or "") + "/login")
            self.send_header("Set-Cookie",
                             "session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            self.end_headers()
            return

        # Healthcheck — always public
        if p == "/healthcheck":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = json.dumps({"status": "ok", "proxy_port": PROXY_PORT,
                                  "proxy_backend": PROXY_BACKEND,
                                  "auth_enabled": AUTH_ENABLED})
            self.wfile.write(payload.encode())
            return

        # Auth guard
        if not _is_authenticated(self):
            self._redirect((PROXY_PATH_PREFIX or "") + "/login")
            return

        if p in ("/", "/index.html"):
            self._serve_index()
            return

        # Proxy API requests to upstream
        for prefix in API_PREFIXES:
            if p.startswith(prefix):
                _proxy_to_upstream(self, "/" + p.lstrip("/"))
                return

        super().do_GET()

    def do_POST(self):
        p = self._norm()

        # Handle login form submission
        if p in ("/login", "/login/"):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            params = urllib.parse.parse_qs(raw)
            username = params.get("username", [""])[0]
            password = params.get("password", [""])[0]

            if (AUTH_ENABLED
                    and username == DASHBOARD_USERNAME
                    and password == DASHBOARD_PASSWORD):
                token = _create_session()
                self.send_response(302)
                # Redirect to dashboard root with trailing slash
                self.send_header("Location", (PROXY_PATH_PREFIX or "") + "/")
                self.send_header("Set-Cookie",
                                 f"session={token}; Path=/; HttpOnly; SameSite=Strict")
                self.end_headers()
            elif not AUTH_ENABLED:
                self._redirect((PROXY_PATH_PREFIX or "") + "/")
            else:
                self._redirect((PROXY_PATH_PREFIX or "") + "/login?error=1")
            return

        # Auth guard for all other POST
        if not _is_authenticated(self):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode())
            return

        # Proxy POST requests to upstream
        for prefix in API_PREFIXES:
            if p.startswith(prefix):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length > 0 else b""
                ct = self.headers.get("Content-Type", "application/json")
                _proxy_to_upstream(self, "/" + p.lstrip("/"),
                                   method="POST", body=body, content_type=ct)
                return

        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        p = self._norm()

        # Auth guard
        if not _is_authenticated(self):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode())
            return

        # Proxy DELETE to upstream (e.g. /api/admin/keys/{key_id})
        for prefix in API_PREFIXES:
            if p.startswith(prefix):
                _proxy_to_upstream(self, "/" + p.lstrip("/"), method="DELETE")
                return

        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        # Suppress default access logs to keep output clean
        pass


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True


def main():
    server = ThreadedServer(("0.0.0.0", PROXY_PORT), DashboardHandler)
    backend_info = f" -> {PROXY_BACKEND}" if PROXY_BACKEND else " (same-origin)"
    auth_info = f" [auth: {DASHBOARD_USERNAME}]" if AUTH_ENABLED else " [auth: disabled]"
    print(f"Dashboard server on :{PROXY_PORT}{backend_info}{auth_info}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
