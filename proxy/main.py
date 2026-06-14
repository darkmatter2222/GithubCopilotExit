"""
LLM Proxy — OpenAI-compatible endpoint backed by Ollama.

Responsibilities:
- Clamp temperature to >= 0.6 (Qwen3 thinking mode requires >= 0.6;
  VS Code Copilot sends 0.1 by default which breaks the model).
- Strip unsupported parameters before forwarding.
- Pass tool/function calling schemas through unchanged.
- No authentication required.
- Streams responses when the client requests streaming.
- Tracks token throughput stats at /stats (JSON) and /dashboard (live HTML).
"""

import os
import json
import time
import uuid
import logging
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proxy")

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
SERVED_MODEL = os.environ.get("SERVED_MODEL_NAME", "qwen3")
MIN_TEMPERATURE = float(os.environ.get("MIN_TEMPERATURE", "0.6"))

from tracker import TokenTracker

tracker = TokenTracker()


app = FastAPI(title="LLM Proxy", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def clamp_temperature(body: dict) -> dict:
    """Ensure temperature is at least MIN_TEMPERATURE."""
    temp = body.get("temperature")
    if temp is not None and temp < MIN_TEMPERATURE:
        log.info(f"Clamping temperature {temp} -> {MIN_TEMPERATURE}")
        body["temperature"] = MIN_TEMPERATURE
    return body


def normalize_model(body: dict) -> dict:
    """Accept any model name the client sends and rewrite to SERVED_MODEL."""
    original = body.get("model", SERVED_MODEL)
    if original != SERVED_MODEL:
        log.info(f"Rewriting model '{original}' -> '{SERVED_MODEL}'")
        body["model"] = SERVED_MODEL
    return body


def prepare_body(body: dict) -> dict:
    body = clamp_temperature(body)
    body = normalize_model(body)
    return body


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": SERVED_MODEL,
                "object": "model",
                "created": 1749000000,
                "owned_by": "local",
                "capabilities": {
                    "tool_calls": True,
                    "vision": True,
                },
                "context_length": 262144,
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    body = prepare_body(body)

    stream = body.get("stream", False)
    target_url = f"{OLLAMA_BASE}/v1/chat/completions"

    headers = {"Content-Type": "application/json"}

    # timeout=None: Qwen3 thinking-mode responses can take 10+ minutes on long
    # generations. The proxy runs on localhost so there is no network risk.
    # A hard timeout here caused ERR_INCOMPLETE_CHUNKED_ENCODING at exactly 300s.
    if stream:
        request_id = tracker.new_request_id()
        tracker.start_request(request_id, SERVED_MODEL)

        async def generate():
            token_count = 0
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "POST", target_url, json=body, headers=headers
                    ) as resp:
                        async for chunk in resp.aiter_bytes():
                            # Parse SSE data lines to count tokens
                            for line in chunk.split(b"\n"):
                                decoded = line.strip().decode("utf-8", errors="replace")
                                if decoded.startswith("data: "):
                                    data_part = decoded[len("data: "):]
                                    if data_part == "[DONE]":
                                        continue
                                    try:
                                        payload = json.loads(data_part)
                                        content = (payload.get("choices", [{}])[0]
                                                   .get("delta", {})
                                                   .get("content", ""))
                                        if content:
                                            token_count += 1
                                            tracker.record_token(request_id)
                                    except (json.JSONDecodeError, KeyError):
                                        pass
                            yield chunk
            finally:
                log.info(
                    f"[{request_id}] completed — {token_count} tokens generated"
                )
                tracker.finish_request(request_id)

        return StreamingResponse(generate(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(target_url, json=body, headers=headers)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type="application/json",
        )


@app.post("/v1/completions")
async def completions(request: Request):
    body = await request.json()
    body = prepare_body(body)
    stream = body.get("stream", False)
    target_url = f"{OLLAMA_BASE}/v1/completions"
    headers = {"Content-Type": "application/json"}

    if stream:
        async def generate():
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", target_url, json=body, headers=headers
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk

        return StreamingResponse(generate(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(target_url, json=body, headers=headers)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type="application/json",
        )


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    body = normalize_model(body)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{OLLAMA_BASE}/v1/embeddings",
            json=body,
            headers={"Content-Type": "application/json"},
        )
    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")


# ── Live stats dashboard ───────────────────────────────────────────────

@app.get("/stats")
async def stats_json():
    """Real-time token throughput stats (machine-readable JSON)."""
    return tracker.get_active_summary()


@app.get("/dashboard", response_class=HTMLResponse)
async def stats_dashboard():
    """Live-updating HTML dashboard showing real-time token throughput."""
    return DASHBOARD_HTML_PAGE


DASHBOARD_HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>LLM Proxy — Live Token Throughput</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #e6edf3; padding: 24px; min-height: 100vh; }
  h1 { font-size: 1.4rem; margin-bottom: 4px; }
  .subtitle { color: #8b949e; font-size: 0.85rem; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; margin-bottom: 16px; }
  .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; }
  .metric h2 { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: #8b949e; margin-bottom: 6px; }
  .metric .value { font-size: 2.2rem; font-weight: 700; }
  .metric.highlight .value { color: #58a6ff; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #21262d; font-size: 0.85rem; }
  th { color: #8b949e; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.05em; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem; font-weight: 600; }
  .badge-active { background: #124a1c; color: #3fb950; }
  .badge-done   { background: #1f2937; color: #8b949e; }
</style>
</head>
<body>
  <h1>&#x1f4ca; Live Token Throughput</h1>
  <p class="subtitle">Auto-refreshes every 2 seconds · http://localhost:8001/dashboard</p>

  <div class="card metrics">
    <div class="metric highlight">
      <h2>Combined Tokens/sec</h2>
      <div class="value" id="tps">&#8212;</div>
    </div>
    <div class="metric">
      <h2>Active Requests</h2>
      <div class="value" id="active">&#8212;</div>
    </div>
    <div class="metric">
      <h2>Total Tokens This Session</h2>
      <div class="value" id="total">&#8212;</div>
    </div>
  </div>

  <div class="card">
    <table>
      <thead><tr><th>ID</th><th>Model</th><th>Tokens</th><th>TPS</th><th>Status</th></tr></thead>
      <tbody id="requests-table"><tr><td colspan="5" style="color:#8b949e;text-align:center;">waiting&#x2026;</td></tr></tbody>
    </table>
  </div>

<script>
async function refresh() {
  try {
    const res = await fetch('/stats');
    const data = await res.json();
    document.getElementById('tps').textContent = (data.combined_tps ?? 0).toFixed(1);
    document.getElementById('active').textContent = data.active_requests;
    document.getElementById('total').textContent = data.total_tokens_generated.toLocaleString();
    const tbody = document.getElementById('requests-table');
    if (!data.requests || data.requests.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:#8b949e;text-align:center;">waiting&#x2026;</td></tr>';
      return;
    }
    tbody.innerHTML = data.requests.map(r =>
      `<tr>
        <td>${r.id}</td><td>${r.model}</td><td>${r.tokens_out}</td>
        <td>${(r.tps||0).toFixed(1)}</td>
        <td>${r.active ? '<span class="badge badge-active">live</span>' : '<span class="badge badge-done">done</span>'}</td>
      </tr>`
    ).join('');
  } catch(e) { console.warn('refresh error', e); }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""


@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{OLLAMA_BASE}/api/tags")
            ollama_ok = r.status_code == 200
    except Exception:
        ollama_ok = False
    return {"status": "ok" if ollama_ok else "degraded", "ollama": ollama_ok}


# Azure OpenAI compatibility shims
@app.get("/openai/deployments/{deployment}/chat/completions")
@app.post("/openai/deployments/{deployment}/chat/completions")
async def azure_chat(deployment: str, request: Request):
    return await chat_completions(request)


@app.get("/openai/models")
async def azure_models():
    return await list_models()
