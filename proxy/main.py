"""
LLM Proxy — Dynamic OpenAI-compatible gateway for Ollama + vLLM.

Architecture:
  - BackendRouter auto-discovers models from Ollama (/api/tags) and optional vLLM (/v1/models)
  - Single /v1/chat/completions endpoint routes requests by model name — no hardcoding
  - Ollama auto-loads models from disk on first request (zero manual intervention)
  - Temperature clamping for Qwen3 thinking-mode compatibility
  - Thinking suppression workaround for Ollama tool-calling bug (inject reasoning_effort=none)
  - delta.reasoning → delta.content remap for vLLM reasoning-parser responses
  - Token tracking (in-memory) + MongoDB persistence for history/analytics
  - /stats JSON + /dashboard live HTML analytics
"""

import json
import logging
import os
import time
import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv

_env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)
elif os.path.exists(".env"):
    load_dotenv(".env")

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import psutil

from router import BackendRouter
from tracker import TokenTracker, set_db
from db import SessionDB
import cost_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proxy")

# ── Configuration ─────────────────────────────────────────────────────────────

MIN_TEMPERATURE = float(os.environ.get("MIN_TEMPERATURE", "0.6"))

# Inject reasoning_effort=none for all requests unless the client explicitly
# enables thinking. This suppresses heavy <think> chains that break tool-calling.
# See: https://github.com/ollama/ollama/issues/10976
DISABLE_THINKING = os.environ.get("DISABLE_THINKING_FOR_TOOLS", "true").lower() in (
    "true", "1", "yes"
)

# ── Global singletons ─────────────────────────────────────────────────────────

router = BackendRouter()
tracker = TokenTracker()
session_db = SessionDB()
set_db(session_db)

# System stats cache (refreshed every 5s to avoid expensive subprocess calls)
_sys_cache: dict = {}
_sys_cache_ts: float = 0.0
_SYS_TTL = 5.0


# ── Application lifecycle ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await session_db.ensure_connection()
    if session_db.enabled:
        log.info("MongoDB persistence enabled")
    else:
        log.warning("MongoDB not available — running memory-only (set MONGO_URI in .env)")
    await router.start()
    yield
    await router.stop()
    await session_db.close()


app = FastAPI(title="LLM Proxy", version="3.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request body transformations ──────────────────────────────────────────────

def clamp_temperature(body: dict) -> dict:
    """Ensure temperature >= MIN_TEMPERATURE (Qwen3 thinking mode requires >= 0.6)."""
    temp = body.get("temperature")
    if temp is not None and isinstance(temp, (int, float)) and temp < MIN_TEMPERATURE:
        log.debug("Clamping temperature %.2f → %.2f", temp, MIN_TEMPERATURE)
        body["temperature"] = MIN_TEMPERATURE
    return body


def suppress_thinking(body: dict) -> dict:
    """
    Inject reasoning_effort=none unless the client explicitly requested thinking.

    Problem: Ollama/vLLM may enable thinking by default for capable models, producing
    heavy <think> chains (100+ seconds) that appear empty to the caller or break
    tool-calling. We suppress thinking globally and let the client opt back in.

    Client can re-enable thinking by:
      - Setting reasoning_effort to any non-null value ("auto", "high", "low")
      - Sending thinking={"type":"enabled"} (Anthropic-style)
    """
    if not DISABLE_THINKING:
        return body
    if body.get("reasoning_effort") is not None:
        return body  # client explicitly set it — leave alone
    client_thinking = body.get("thinking")
    if (
        isinstance(client_thinking, dict)
        and client_thinking.get("type") == "enabled"
    ):
        return body  # client enabled Anthropic-style thinking
    body["reasoning_effort"] = "none"
    return body


def remap_reasoning_to_content(chunk: bytes) -> bytes:
    """
    Transform delta.reasoning → delta.content in SSE chunks.

    vLLM with --reasoning-parser routes thinking output into delta.reasoning instead
    of delta.content. VS Code only reads delta.content, so responses appear empty.
    This remap makes thinking content visible.
    """
    if b'"reasoning"' not in chunk:
        return chunk  # fast path — most chunks don't need remapping
    lines = chunk.split(b"\n")
    new_lines = []
    changed = False
    for line in lines:
        decoded = line.strip().decode("utf-8", errors="replace")
        if decoded.startswith("data: ") and decoded != "data: [DONE]":
            try:
                payload = json.loads(decoded[6:])
                modified = False
                for choice in payload.get("choices", []):
                    delta = choice.get("delta", {})
                    reasoning = delta.get("reasoning")
                    if reasoning and not delta.get("content"):
                        delta["content"] = reasoning
                        del delta["reasoning"]
                        modified = True
                if modified:
                    new_lines.append(f"data: {json.dumps(payload)}".encode())
                    changed = True
                    continue
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
        new_lines.append(line)
    return b"\n".join(new_lines) if changed else chunk


def prepare_body(body: dict) -> tuple[dict, str]:
    """Apply all transformations; returns (modified_body, client_model_alias)."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return body, "unknown"
    client_alias = body.get("model", "")
    body = clamp_temperature(body)
    body = suppress_thinking(body)
    return body, client_alias


# ── System stats ──────────────────────────────────────────────────────────────

async def get_system_stats() -> dict:
    """Gather CPU/RAM/disk/GPU stats. Cached for _SYS_TTL seconds."""
    global _sys_cache, _sys_cache_ts
    now = time.time()
    if _sys_cache and (now - _sys_cache_ts) < _SYS_TTL:
        return _sys_cache

    result: dict = {}

    # CPU / RAM / disk via psutil
    try:
        cpu_pct = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        result["cpu_percent"] = round(cpu_pct, 1)
        result["ram_used_gb"] = round(mem.used / 1024 ** 3, 1)
        result["ram_total_gb"] = round(mem.total / 1024 ** 3, 1)
        result["ram_percent"] = mem.percent
        result["disk_used_gb"] = round(disk.used / 1024 ** 3, 1)
        result["disk_total_gb"] = round(disk.total / 1024 ** 3, 1)
        result["disk_percent"] = disk.percent
    except Exception as exc:
        log.warning("psutil failed: %s", exc)

    # GPU via nvidia-smi
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        gpus = []
        for line in stdout.decode().strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                def _i(s: str) -> int:
                    c = s.strip().strip("[]")
                    return int(float(c)) if c and c.upper() not in ("N/A", "") else 0
                gpus.append({
                    "name": parts[0],
                    "util_percent": _i(parts[1]),
                    "mem_used_mb": _i(parts[2]),
                    "mem_total_mb": _i(parts[3]),
                    "temp_c": _i(parts[4]),
                })
        result["gpus"] = gpus
    except Exception as exc:
        log.debug("nvidia-smi unavailable: %s", exc)
        result["gpus"] = []

    # Ollama loaded models
    try:
        ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{ollama_url}/api/tags")
        if r.status_code == 200:
            result["ollama_models"] = [
                {
                    "name": m.get("name", ""),
                    "size_mb": round(m.get("size", 0) / 1024 ** 2, 1),
                }
                for m in r.json().get("models", [])
            ]
        else:
            result["ollama_models"] = []
    except Exception:
        result["ollama_models"] = []

    _sys_cache.clear()
    _sys_cache.update(result)
    _sys_cache_ts = time.time()
    return _sys_cache


# ── Core streaming proxy ──────────────────────────────────────────────────────

async def stream_completions(
    body: dict,
    target_url: str,
    request_id: str,
    client_alias: str,
) -> StreamingResponse:
    """Stream a chat completion from target_url, tracking tokens along the way."""
    tracker.start_request(request_id, client_alias)

    async def generate():
        token_count = 0
        tool_call_seen = False
        fallback_injected = False
        stream_done = False
        saved_id = "fallback"
        had_exception = False

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", target_url, json=body) as resp:
                    async for chunk in resp.aiter_bytes():
                        for line in chunk.split(b"\n"):
                            stripped = line.strip().decode("utf-8", errors="replace")
                            if not stripped.startswith("data: "):
                                continue
                            data_part = stripped[6:]
                            if data_part == "[DONE]":
                                stream_done = True
                                continue
                            try:
                                payload = json.loads(data_part)
                                saved_id = payload.get("id", saved_id)
                                choices = payload.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content") or delta.get("reasoning") or ""
                                    if content:
                                        token_count += 1
                                        tracker.record_token(request_id)
                                    if delta.get("tool_calls"):
                                        tool_call_seen = True
                                usage = payload.get("usage")
                                if usage:
                                    tracker.update_from_response(request_id, payload)
                            except (json.JSONDecodeError, KeyError):
                                pass

                        chunk = remap_reasoning_to_content(chunk)

                        # Inject fallback message before [DONE] when model produces no output
                        if (
                            token_count == 0
                            and not tool_call_seen
                            and b"data: [DONE]" in chunk
                            and not fallback_injected
                        ):
                            log.warning("[%s] empty content — injecting fallback", request_id)
                            synth = (
                                f"data: {{\"id\":\"{saved_id}\",\"object\":\"chat.completion.chunk\","
                                f"\"choices\":[{{\"index\":0,\"delta\":{{\"content\":\"[Model produced no output — please retry]\"}},\"finish_reason\":null}}]}}\n\n"
                            )
                            yield synth.encode()
                            fallback_injected = True

                        yield chunk

        except Exception as exc:
            had_exception = True
            log.error("[%s] stream error: %s", request_id, exc)
            tracker.record_error(request_id, str(exc))
            yield (
                f"data: {{\"id\":\"{saved_id}\",\"object\":\"chat.completion.chunk\","
                f"\"choices\":[{{\"index\":0,\"delta\":{{\"content\":\"[Stream interrupted — please retry]\"}},\"finish_reason\":\"stop\"}}]}}\n\ndata: [DONE]\n\n"
            ).encode()
            return
        finally:
            if not had_exception:
                tracker.finish_request(request_id)

        # Guard: stream ended without [DONE] and no content
        if not had_exception and not stream_done and token_count == 0 and not tool_call_seen:
            log.warning("[%s] stream ended without [DONE] — injecting fallback", request_id)
            yield (
                f"data: {{\"id\":\"{saved_id}\",\"object\":\"chat.completion.chunk\","
                f"\"choices\":[{{\"index\":0,\"delta\":{{\"content\":\"[Model produced no output — please retry]\"}},\"finish_reason\":\"stop\"}}]}}\n\ndata: [DONE]\n\n"
            ).encode()

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/v1/models")
async def list_models():
    """List all models discovered from Ollama and vLLM (OpenAI-compatible)."""
    models = await router.get_all_models()
    return {
        "object": "list",
        "data": [m.as_openai_entry() for m in models],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    Dynamic chat completions endpoint.
    Routes to Ollama or vLLM based on the model name in the request body.
    Ollama auto-loads models from disk on first request — no manual configuration needed.
    """
    body = await request.json()
    body, client_alias = prepare_body(body)

    backend = await router.get_backend(body.get("model", ""))
    target_url = f"{backend.base_url}/v1/chat/completions"

    log.info(
        "-> %s  model=%s  backend=%s",
        client_alias, body.get("model", "?"), backend.backend,
    )

    if body.get("stream", False):
        request_id = tracker.new_request_id()
        return await stream_completions(body, target_url, request_id, client_alias)

    # Non-streaming
    request_id = tracker.new_request_id()
    tracker.start_request(request_id, client_alias)
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            resp = await client.post(target_url, json=body)
        try:
            resp_json = json.loads(resp.content) if resp.content else {}
            tracker.update_from_response(request_id, resp_json)
        except (json.JSONDecodeError, KeyError):
            pass
        tracker.finish_request(request_id)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type="application/json",
        )
    except Exception as exc:
        tracker.record_error(request_id, str(exc))
        raise


@app.post("/v1/completions")
async def completions(request: Request):
    """Legacy text completions — proxy to Ollama."""
    body = await request.json()
    body, _ = prepare_body(body)
    backend = await router.get_backend(body.get("model", ""))
    target_url = f"{backend.base_url}/v1/completions"

    if body.get("stream", False):
        async def gen():
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", target_url, json=body) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
        return StreamingResponse(gen(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(target_url, json=body)
    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """Embeddings — proxy to Ollama."""
    body = await request.json()
    backend = await router.get_backend(body.get("model", ""))
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(
            f"{backend.base_url}/v1/embeddings",
            json=body,
            headers={"Content-Type": "application/json"},
        )
    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")


# ── Router management ─────────────────────────────────────────────────────────

@app.post("/api/router/refresh")
async def force_refresh():
    """Force immediate re-discovery of all backends."""
    await router.refresh()
    models = await router.get_all_models()
    return {"status": "ok", "model_count": len(models), "models": [m.model_id for m in models]}


@app.get("/api/models/running")
async def models_running():
    """Return models currently loaded in Ollama VRAM."""
    return {"data": await router.get_ollama_running()}


@app.get("/api/models/enriched")
async def enriched_models():
    """Return models with full metadata (architecture, params, capabilities, etc.)."""
    models = await router.get_enriched_models()
    running_data = await router.get_ollama_running()
    running_names = {m.get("name", "") for m in running_data}
    # Mark which models are in VRAM
    for m in models:
        mid = m["id"]
        base_name = mid.split(":")[0]
        m["is_loaded"] = mid in running_names or base_name in running_names
    return {"data": models, "last_refresh": router.last_refresh_ts}


@app.post("/api/models/check-updates")
async def check_model_updates():
    """Compare local model digests against ollama.com global registry."""
    return await router.check_updates()


# ── Live stats ────────────────────────────────────────────────────────────────

@app.get("/stats")
async def stats_json():
    """Real-time token throughput stats plus system resource data."""
    data = tracker.get_active_summary()
    total_in = data.get("total_input_tokens", 0) or 0
    total_out = data.get("total_output_tokens", 0) or 0
    tiers = cost_engine.get_tier_summary(total_in, total_out)
    data["live_session_cost"] = {t: round(v, 4) for t, v in tiers.items()}
    try:
        data["sys"] = await get_system_stats()
    except Exception as exc:
        log.warning("System stats failed: %s", exc)
        data["sys"] = {}
    return data


# ── Historical API (MongoDB-backed) ──────────────────────────────────────────

@app.get("/api/history")
async def get_history(days: int = 30, limit: int = 200):
    docs = await session_db.get_requests(limit=limit, days=days)
    return JSONResponse({"count": len(docs), "data": docs})


@app.get("/api/usage/daily")
async def get_daily_usage(days: int = 30):
    rows = await session_db.get_token_usage_by_day(days=days)
    return JSONResponse({"count": len(rows), "data": rows})


@app.get("/api/usage/hourly")
async def get_hourly_usage(days: int = 7):
    rows = await session_db.get_token_usage_by_hour(days=days)
    return JSONResponse({"count": len(rows), "data": rows})


@app.get("/api/stats/summary")
async def get_stats_summary(days: int = 30):
    data = await session_db.get_stats_summary(days=days)
    return JSONResponse(data)


# ── Cost analysis API ─────────────────────────────────────────────────────────

@app.get("/api/cost/models")
async def get_cost_models():
    return JSONResponse({"data": cost_engine.format_pricing_table()})


@app.get("/api/cost/summary")
async def get_cost_summary(days: int = 30):
    raw = await session_db.get_cost_summary(days=days)
    if not raw:
        return JSONResponse({})
    in_t = raw.get("total_input_tokens", 0) or 0
    out_t = raw.get("total_output_tokens", 0) or 0
    dur = raw.get("total_duration_secs", 0) or 0
    tiers = cost_engine.get_tier_summary(in_t, out_t)
    local_cost = cost_engine.get_gpu_cost_for_duration(dur)
    return JSONResponse({
        "days": days,
        "total_requests": raw.get("total_requests", 0),
        "total_input_tokens": in_t,
        "total_output_tokens": out_t,
        "total_duration_hours": round(dur / 3600, 2),
        "cloud_costs": {t: cost_engine.dollar_fmt(v) for t, v in tiers.items()},
        "cloud_costs_raw": tiers,
        "per_model_costs_raw": {
            mid: cost_engine.calculate_cloud_cost(in_t, out_t, mid)
            for mid in cost_engine.MODEL_PRICING
        },
        "local_gpu_cost": round(local_cost, 4),
        "local_gpu_cost_fmt": cost_engine.dollar_fmt(local_cost),
        "gpu_hourly_rate": round(cost_engine.get_gpu_hourly_cost(), 4),
        "savings_vs_high": round(tiers["high"] - local_cost, 4) if tiers["high"] > local_cost else 0,
        "savings_vs_medium": round(tiers["medium"] - local_cost, 4) if tiers["medium"] > local_cost else 0,
    })


@app.get("/api/cost/daily")
async def get_cost_daily(days: int = 30):
    rows = await session_db.get_cost_by_day(days=days)
    enriched = []
    for row in rows:
        in_t = row.get("total_input_tokens", 0) or 0
        out_t = row.get("total_output_tokens", 0) or 0
        dur = row.get("total_duration_secs", 0) or 0
        enriched.append({
            "date": row["date"],
            "input_tokens": in_t,
            "output_tokens": out_t,
            "cloud_high": round(cost_engine.calculate_cloud_cost_by_tier(in_t, out_t, "high"), 4),
            "cloud_medium": round(cost_engine.calculate_cloud_cost_by_tier(in_t, out_t, "medium"), 4),
            "cloud_low": round(cost_engine.calculate_cloud_cost_by_tier(in_t, out_t, "low"), 4),
            "local_gpu": round(cost_engine.get_gpu_cost_for_duration(dur), 4),
        })
    return JSONResponse({"count": len(enriched), "data": enriched})


# ── Health + dashboard ────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    ollama_ok = False
    try:
        ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{ollama_url}/api/tags")
        ollama_ok = r.status_code == 200
    except Exception:
        pass
    models = await router.get_all_models()
    return {
        "status": "ok",
        "ollama": ollama_ok,
        "model_count": len(models),
        "last_refresh": router.last_refresh_ts,
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the analytics dashboard."""
    d = os.path.dirname(__file__)
    for path in [
        os.path.join(d, "dashboard.html"),
        os.path.join(d, "..", "dashboard", "index.html"),
    ]:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    return HTMLResponse(
        "<h1>Dashboard not found</h1><p>Deploy dashboard/index.html alongside the proxy.</p>"
    )


# ── Azure OpenAI compatibility shims ─────────────────────────────────────────

@app.post("/openai/deployments/{deployment}/chat/completions")
@app.get("/openai/deployments/{deployment}/chat/completions")
async def azure_chat(deployment: str, request: Request):
    return await chat_completions(request)


@app.get("/openai/models")
async def azure_models():
    return await list_models()
