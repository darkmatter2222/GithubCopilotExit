FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Proxy source files
COPY *.py ./

# Dashboard — served at /dashboard
# Looks for dashboard.html here, then ../dashboard/index.html
COPY dashboard.html ./

# ── Runtime defaults (override via docker run -e or .env mount) ────────────
# OLLAMA_BASE_URL  — Ollama API (host network: use host.docker.internal or actual IP)
# VLLM_BASE_URL    — Optional vLLM comma-separated URLs
# MIN_TEMPERATURE  — Minimum temperature clamp for Qwen3 thinking mode
# MONGO_URI        — MongoDB connection string (leave blank for memory-only)
# MONGO_DB         — MongoDB database name
ENV OLLAMA_BASE_URL=http://host.docker.internal:11434
ENV VLLM_BASE_URL=
ENV MIN_TEMPERATURE=0.6
ENV DISABLE_THINKING_FOR_TOOLS=true
ENV ROUTER_REFRESH_S=30

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--log-level", "info"]
