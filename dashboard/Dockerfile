# ──────────────────────────────────────────────────────────────
#  Standalone Dashboard Container — lightweight HTTP server
#
#  Serves dashboard/index.html on a separate host from the proxy.
#  PROXY_URL env var is injected into index.html at startup so
#  that the JS fetch calls (pFetch) reach back to the DGX Spark
#  proxy rather than localhost.
#
#  Build:
#    docker build -t gcopilot-dashboard \
#      --build-arg PROXY_URL=http://dgxspark:8001 \
#      -f dashboard/Dockerfile .
#
#  Or set at runtime (container re-reads on each request):
#    docker run -d --name gcopilot-dashboard \
#      --network host \
#      -e PROXY_URL=http://dgxspark:8001 \
#      gcopilot-dashboard
# ─────────════════════════════════════════════════════════════=
FROM python:3.12-alpine

WORKDIR /srv

COPY dashboard/serve.py .
COPY dashboard/index.html .

# Runtime injection: PROXY_URL points dashboard fetch() calls to the proxy host.
# When empty (default), all fetches are same-origin (proxy runs on same host).
# Set to e.g. "http://dgxspark:8001" when dashboard runs on a different server.
ENV PROXY_URL=""
ENV DASHBOARD_PORT=3000
ENV REFRESH_S=5

EXPOSE ${DASHBOARD_PORT}

HEALTHCHECK --interval=15s --timeout=5s --start-period=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3000/healthcheck')" || exit 1

CMD ["python", "serve.py"]
