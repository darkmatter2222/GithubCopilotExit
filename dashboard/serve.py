"""
Standalone HTTP server for the LLM Proxy Dashboard.

Injects PROXY_URL into index.html at serve time so JS pFetch() calls
target the correct proxy host (e.g. http://dgxspark:8001) when the
dashboard container runs on a different machine than the proxy+.

Environment variables:
  PROXY_URL / DASHBOARD_PROXY_URL — proxy URL to inject (default: "" for
                                    same-origin, e.g. "http://dgxspark:8001")
  PROXY_PORT / DASHBOARD_PORT     — listen port (default: 3000)
"""

import http.server
import io
import json
import os
import socketserver

PROXY_PORT = int(os.environ.get("PROXY_PORT", os.environ.get("DASHBOARD_PORT", "3000")))
PROXY_URL  = os.environ.get("PROXY_URL", os.environ.get("DASHBOARD_PROXY_URL", ""))

# Source HTML template — contains the placeholder __PROXY_URL_PLACEHOLDER__
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "index.html")

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Serves index.html with PROXY_URL injected, plus healthcheck."""

    def __init__(self, *args, **kwargs):
        # Serve from the dashboard directory
        super().__init__(*args, directory=os.path.dirname(__file__), **kwargs)

    def _serve_index(self):
        try:
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                html = f.read()
        except FileNotFoundError:
            html = "<h1>Dashboard not found</h1>"

        # Inject a tiny script block before </head> that sets window.__PROXY_URL
        injection = f'<script>window.__PROXY_URL="{PROXY_URL}";</script>'
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
                                  "proxy_url": PROXY_URL})
            self.wfile.write(payload.encode())
            return
        # Fallback: serve other files from disk (static assets if any)
        super().do_GET()


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True


def main():
    server = ThreadedServer(("0.0.0.0", PROXY_PORT), DashboardHandler)
    proxy_info = f" -> {PROXY_URL}" if PROXY_URL else " (same-origin)"
    print(f"Dashboard server on :{PROXY_PORT}{proxy_info}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
