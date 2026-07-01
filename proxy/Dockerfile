FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Set version metadata from build args
ARG BUILD_VERSION="unknown"
ARG BUILD_TIMESTAMP=""
ENV PROXY_VERSION=${BUILD_VERSION}
ENV BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

# Proxy source files
COPY *.py ./

# ── Runtime defaults (override via docker run -e or .env mount) ────────────
# OLLAMA_BASE_URL  — Ollama API endpoint (DGX Spark: http://192.168.86.39:11434)
# VLLM_BASE_URL    — Optional vLLM comma-separated URLs
# MIN_TEMPERATURE  — Minimum temperature clamp for Qwen3 thinking mode
# MONGO_URI        — MongoDB connection string (leave blank for memory-only)
# MONGO_DB         — MongoDB database name
ENV OLLAMA_BASE_URL=http://192.168.86.39:11434
ENV VLLM_BASE_URL=
ENV MIN_TEMPERATURE=0.6
ENV DISABLE_THINKING_FOR_TOOLS=true
ENV ROUTER_REFRESH_S=30

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--log-level", "info"]
