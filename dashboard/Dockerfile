# ──────────────────────────────────────────────────────────────
#  Standalone Dashboard Container — lightweight HTTP server
#
#  Serves dashboard/index.html on a separate host from the proxy.
#  Injects two env-driven values into index.html at runtime:
#    PROXY_URL          — for browser pFetch() to reach proxy directly (same-host)
#    PROXY_PATH_PREFIX  — path prefix for browser fetches when behind nginx ingress
#                         (e.g. "/copilot" → /copilot/stats, /copilot/v1/models)
#    PROXY_BACKEND      — server-side upstream target for serve.py to proxy API calls
#                         to DGX Spark (use IP 192.168.86.39 — hostname not resolvable)
#
#  Build:
#    docker build -t gcopilot-dashboard -f dashboard/Dockerfile .
#
#  Remote deployment behind nginx at /copilot/:
#    docker run -d --name gcopilot-dashboard \
#      --network docucraft_docucraft-network \
#      -e PROXY_BACKEND=http://192.168.86.39:8001 \
#      -e PROXY_PATH_PREFIX=/copilot \
#      gcopilot-dashboard
#
#  Same-host deployment (DGX Spark, direct browser access):
#    docker run -d --name gcopilot-dashboard \
#      -e PROXY_URL=http://localhost:8001 \
#      gcopilot-dashboard
# ─────────════════════════════════════════════════════════════=
FROM python:3.12-alpine

WORKDIR /srv

COPY dashboard/serve.py .
COPY dashboard/index.html .

ENV PROXY_URL=""
ENV PROXY_PATH_PREFIX=""
ENV DASHBOARD_PORT=3000
ENV REFRESH_S=5

EXPOSE ${DASHBOARD_PORT}

HEALTHCHECK --interval=15s --timeout=5s --start-period=3s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('DASHBOARD_PORT','3000') + '/healthcheck')" || exit 1

CMD ["python", "serve.py"]
