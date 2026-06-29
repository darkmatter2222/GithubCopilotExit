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

import http.server
import io
import json
import os
import socketserver
import urllib.request
import urllib.error
from urllib.parse import urljoin

PROXY_PORT = int(os.environ.get("PROXY_PORT", os.environ.get("DASHBOARD_PORT", "3000")))
# Server-side upstream target — ALWAYS set when deploying behind nginx ingress
PROXY_BACKEND = os.environ.get(
    "PROXY_BACKEND", os.environ.get("PROXY_URL", os.environ.get("DASHBOARD_PROXY_URL", "")))
# What to inject into HTML for browser pFetch() calls.
# Empty (default) → browser uses relative paths → serve.py proxies server-side.
HTML_PROXY_URL = os.environ.get("HTML_PROXY_URL", "")

# Source HTML template — contains the placeholder __PROXY_URL_PLACEHOLDER__
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "index.html")

# Paths that the dashboard JS pFetch() calls hit.
API_PREFIXES = ("/stats", "/v1/", "/api/")

# When deployed behind nginx at /copilot/, all browser fetch() paths need
# the prefix prepended (e.g. /stats → /copilot/stats).  Set this env var
# (e.g. PROXY_PATH_PREFIX=/copilot) or serve.py will autodetect via X-Forwarded-Prefix.
PROXY_PATH_PREFIX = os.environ.get("PROXY_PATH_PREFIX", "")


def _proxy_to_upstream(handler, path):
    """Forward a request to the upstream PROXY_BACKEND and stream back the response."""
    if not PROXY_BACKEND:
        handler.send_response(502)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps({"error": "PROXY_BACKEND not configured"}).encode())
        return

    try:
        upstream_url = urljoin(PROXY_BACKEND, path)
        req = urllib.request.Request(upstream_url)
        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read()
        handler.send_response(resp.status)
        for hdr_name, hdr_val in resp.getheaders():
            if hdr_name.lower() == "transfer-encoding":
                continue
            handler.send_header(hdr_name, hdr_val)
        handler.end_headers()
        handler.wfile.write(body)
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
            json.dumps({"error": f"cannot reach proxy at {PROXY_BACKEND}", "reason": str(e.reason)}).encode()
        )


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Serves index.html with HTML_PROXY_URL injected, healthcheck, and API proxy."""

    def __init__(self, *args, **kwargs):
        # Serve from the dashboard directory
        super().__init__(*args, directory=os.path.dirname(__file__), **kwargs)

    def _serve_index(self):
        try:
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                html = f.read()
        except FileNotFoundError:
            html = "<h1>Dashboard not found</h1>"

        # Inject window.__PROXY_URL for browser pFetch() calls.
        # Default is empty → browser uses relative paths (serve.py proxies server-side).
        # Also inject __BASE_PATH when behind nginx reverse-proxy prefix (e.g., /copilot/).
        injection = (f'<script>window.__PROXY_URL="{HTML_PROXY_URL}";'
                     f'window.__BASE_PATH="{PROXY_PATH_PREFIX}";</script>')
        if "</head>" in html:
            html = html.replace("</head>", injection + "\n</head>", 1)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_index()
            return
        elif self.path == "/healthcheck":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = json.dumps({"status": "ok", "proxy_port": PROXY_PORT,
                                  "proxy_backend": PROXY_BACKEND})
            self.wfile.write(payload.encode())
            return
        # Proxy API requests to upstream proxy so remote dashboard can fetch live data
        for prefix in API_PREFIXES:
            if self.path.startswith(prefix):
                _proxy_to_upstream(self, self.path)
                return
        # Fallback: serve other files from disk (static assets if any)
        super().do_GET()


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True


def main():
    server = ThreadedServer(("0.0.0.0", PROXY_PORT), DashboardHandler)
    backend_info = f" -> {PROXY_BACKEND}" if PROXY_BACKEND else " (same-origin)"
    print(f"Dashboard server on :{PROXY_PORT}{backend_info}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
