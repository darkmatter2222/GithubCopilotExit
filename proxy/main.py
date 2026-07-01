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
  - /stats JSON analytics (dashboard served separately on the remote host)
"""

import json
import logging
import os
import time
import asyncio
import hmac
import secrets as _secrets_mod
from contextlib import asynccontextmanager

from dotenv import load_dotenv

_env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path)
elif os.path.exists(".env"):
    load_dotenv(".env")

# Version tracking
PROXY_VERSION = "2026.06.30.1"
BUILD_TIMESTAMP = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
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

# ── Auth configuration ────────────────────────────────────────────────────────

# When true, all /v1/* /stats /api/* endpoints require a valid API key.
API_KEY_REQUIRED = os.environ.get("API_KEY_REQUIRED", "false").lower() in ("true", "1", "yes")

# Comma-separated static API keys (checked in addition to MongoDB-stored keys).
STATIC_API_KEYS: frozenset[str] = frozenset(
    k.strip() for k in os.environ.get("PROXY_API_KEYS", "").split(",") if k.strip()
)

# Admin credentials for /api/admin/* management endpoints.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

_http_basic = HTTPBasic(auto_error=True)


def verify_admin(creds: HTTPBasicCredentials = Depends(_http_basic)) -> HTTPBasicCredentials:
    """FastAPI dependency: require valid HTTP Basic admin credentials."""
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin auth not configured — set ADMIN_USERNAME and ADMIN_PASSWORD")
    ok_user = hmac.compare_digest(creds.username.encode(), ADMIN_USERNAME.encode())
    ok_pass = hmac.compare_digest(creds.password.encode(), ADMIN_PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": 'Basic realm="LLM Proxy Admin"'},
        )
    return creds

# ── Global singletons ─────────────────────────────────────────────────────────

router = BackendRouter()
tracker = TokenTracker()
session_db = SessionDB()
set_db(session_db)

# GPU stats service — runs on DGX Spark (port 11435) when proxy is remote (the remote host).
# Auto-derived from OLLAMA_BASE_URL if not explicitly set: replace :11434 → :11435.
_ollama_base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
GPU_STATS_URL: str = os.environ.get(
    "GPU_STATS_URL",
    _ollama_base.replace(":11434", ":11435") + "/gpu-stats"
    if ":11434" in _ollama_base and "localhost" not in _ollama_base
    else "",
)
# Override total GPU VRAM (MB) reported when Ollama /api/ps is the only source.
# GB10 Grace Blackwell = 122 GB unified memory → 124928 MB.
GPU_MEM_TOTAL_MB: int = int(os.environ.get("GPU_MEM_TOTAL_MB", "0"))

# System stats cache (refreshed every 5s to avoid expensive subprocess calls)
_sys_cache: dict = {}
_sys_cache_ts: float = 0.0
_SYS_TTL = 5.0

# Delta counters for IO charts (network + disk) — persisted across cache refreshes
_net_prev: dict = {}   # {bytes_sent, bytes_recv, packets_sent, packets_recv, ts}
_disk_prev: dict = {}  # {read_bytes, write_bytes, read_count, write_count, busy_time, ts}


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


# ── API Key Authentication Middleware ─────────────────────────────────────────

# Paths that never require API key authentication.
_AUTH_EXEMPT_PREFIXES = ("/health",)
_AUTH_EXEMPT_EXACT = {"/health", "/"}


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """Enforce API key authentication when API_KEY_REQUIRED=true."""
    if not API_KEY_REQUIRED:
        return await call_next(request)

    path = request.url.path

    # Admin endpoints use their own HTTP Basic Auth — exempt from API key check.
    if path.startswith("/api/admin"):
        return await call_next(request)

    # Health endpoint is always accessible.
    if path in _AUTH_EXEMPT_EXACT or any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return await call_next(request)

    # Extract API key from Authorization: Bearer <key> or X-API-Key header.
    api_key: str = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header[7:].strip()
    if not api_key:
        api_key = request.headers.get("X-API-Key", "").strip()
    if not api_key:
        api_key = request.headers.get("x-api-key", "").strip()

    if not api_key:
        return JSONResponse(
            {"error": "Unauthorized", "detail": "API key required. Provide Authorization: Bearer <key>"},
            status_code=401,
        )

    # Constant-time check against static env-var keys.
    for static_key in STATIC_API_KEYS:
        if hmac.compare_digest(api_key.encode(), static_key.encode()):
            return await call_next(request)

    # Check MongoDB / in-memory dynamic keys.
    try:
        if await session_db.verify_api_key(api_key):
            return await call_next(request)
    except Exception as exc:
        log.warning("API key verification error: %s", exc)

    log.warning("Rejected request with invalid API key (path=%s)", path)
    return JSONResponse(
        {"error": "Forbidden", "detail": "Invalid or revoked API key"},
        status_code=401,
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
    global _sys_cache, _sys_cache_ts, _net_prev, _disk_prev
    now = time.time()
    if _sys_cache and (now - _sys_cache_ts) < _SYS_TTL:
        return _sys_cache

    result: dict = {}

    # CPU / RAM / disk via psutil
    try:
        cpu_pct = psutil.cpu_percent(interval=0.1)
        cpu_per = psutil.cpu_percent(interval=0, percpu=True)  # uses prior interval samples
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        result["cpu_percent"] = round(cpu_pct, 1)
        result["cpu_per_core"] = [round(x, 1) for x in cpu_per]
        result["cpu_count"] = len(cpu_per)
        result["ram_used_gb"] = round(mem.used / 1024 ** 3, 1)
        result["ram_total_gb"] = round(mem.total / 1024 ** 3, 1)
        result["ram_percent"] = mem.percent
        result["disk_used_gb"] = round(disk.used / 1024 ** 3, 1)
        result["disk_total_gb"] = round(disk.total / 1024 ** 3, 1)
        result["disk_percent"] = disk.percent
    except Exception as exc:
        log.warning("psutil failed: %s", exc)

    # ── Network IO delta (bytes/s, packets/s) ──────────────────────────────────────
    try:
        net = psutil.net_io_counters()
        dt_net = now - _net_prev.get("ts", now)
        if dt_net > 0 and _net_prev:
            result["net_rx_bytes_s"] = round((net.bytes_recv - _net_prev.get("bytes_recv", 0)) / dt_net)
            result["net_tx_bytes_s"] = round((net.bytes_sent - _net_prev.get("bytes_sent", 0)) / dt_net)
            result["net_rx_pps"] = int((net.packets_recv - _net_prev.get("packets_recv", 0)) / dt_net)
            result["net_tx_pps"] = int((net.packets_sent - _net_prev.get("packets_sent", 0)) / dt_net)
        else:
            # First call — zero deltas, will compute on next refresh
            for k in ("net_rx_bytes_s", "net_tx_bytes_s", "net_rx_pps", "net_tx_pps"):
                result[k] = 0
        _net_prev = {
            "bytes_sent": net.bytes_sent, "bytes_recv": net.bytes_recv,
            "packets_sent": net.packets_sent, "packets_recv": net.packets_recv,
            "ts": now,
        }
    except Exception as exc:
        log.debug("net_io_counters failed: %s", exc)

    # ── Disk IO delta (nvme0n1 read/write MB/s, busy %) ────────────────────────────
    try:
        dio_all = psutil.disk_io_counters(perdisk=True)
        # Prefer nvme0n1; fall back to aggregated counters if not present
        dio = dio_all.get("nvme0n1") or psutil.disk_io_counters()
        dt_disk = now - _disk_prev.get("ts", now)
        if dt_disk > 0 and _disk_prev:
            result["disk_read_bytes_s"] = round(
                (dio.read_bytes - _disk_prev.get("read_bytes", 0)) / dt_disk
            )
            result["disk_write_bytes_s"] = round(
                (dio.write_bytes - _disk_prev.get("write_bytes", 0)) / dt_disk
            )
            busy_us = dio.busy_time - _disk_prev.get("busy_time", dio.busy_time)
            result["disk_busy_pct"] = round(min(100.0, busy_us / 1e6 * 100 / dt_disk), 1)
        else:
            # First call — zero deltas
            for k in ("disk_read_bytes_s", "disk_write_bytes_s", "disk_busy_pct"):
                result[k] = 0
        _disk_prev = {
            "read_bytes": dio.read_bytes, "write_bytes": dio.write_bytes,
            "busy_time": dio.busy_time, "ts": now,
        }
    except Exception as exc:
        log.debug("disk_io_counters failed: %s", exc)

    # ── GPU stats — three sources in priority order ────────────────────────────
    # 1. Remote GPU stats service (gpu_stats_server.py on DGX Spark, port 11435)
    #    → Real nvidia-smi data: util%, VRAM used/total, temperature
    # 2. Local nvidia-smi (only works when proxy runs on a machine with a GPU)
    #    → Same real data, used when proxy is co-located with the GPU
    # 3. Ollama /api/ps fallback
    #    → Reports VRAM consumed by the loaded model (no real-time util%)
    #    → Binary: 100% util when model loaded, 0% when idle

    gpus_result: list = []

    # Source 1: Remote GPU stats service
    if GPU_STATS_URL and not gpus_result:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(GPU_STATS_URL)
            if r.status_code == 200:
                payload = r.json()
                gpus_result = payload.get("gpus", [])
                if payload.get("loaded_models"):
                    result["loaded_models"] = payload.get("loaded_models", [])
                    result["loaded_model_count"] = payload.get("loaded_model_count", len(result["loaded_models"]))
        except Exception as exc:
            log.debug("GPU stats service unavailable (%s): %s", GPU_STATS_URL, exc)

    # Source 2: Local nvidia-smi
    if not gpus_result:
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            def _i(s: str) -> int:
                c = s.strip().strip("[]")
                try:
                    return int(float(c)) if c and c.upper() not in ("N/A", "") else 0
                except ValueError:
                    return 0

            for line in stdout.decode().strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 5:
                    gpus_result.append({
                        "name": parts[0],
                        "util_percent": _i(parts[1]),
                        "mem_used_mb": _i(parts[2]),
                        "mem_total_mb": _i(parts[3]),
                        "temp_c": _i(parts[4]),
                    })
        except Exception as exc:
            log.debug("nvidia-smi unavailable: %s", exc)

    # Source 3: Ollama /api/ps — modern Ollama (v0.3+) uses size_vram (bytes)
    # No processor/mem_used fields; util is binary (loaded=100%, not loaded=0%)
    loaded_models: list[dict] = []
    try:
        ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{ollama_url}/api/ps")
        if r.status_code == 200:
            loaded_models = [
                {
                    "name": m.get("name", ""),
                    "size_vram": m.get("size_vram", 0),
                    "size_vram_mb": (m.get("size_vram", 0) or 0) // (1024 * 1024),
                }
                for m in r.json().get("models", [])
                if m.get("name")
            ]
            if not gpus_result:
                mem_total_mb = GPU_MEM_TOTAL_MB or 0
                total_vram_mb = sum(m.get("size_vram_mb", 0) for m in loaded_models)
                if total_vram_mb > 0:
                    gpus_result.append({
                        "name": "GPU (DGX Spark)",
                        "util_percent": 100,
                        "mem_used_mb": total_vram_mb,
                        "mem_total_mb": mem_total_mb,
                        "temp_c": 0,
                        "model_count": len(loaded_models),
                        "loaded_models": [m["name"] for m in loaded_models],
                    })
    except Exception as exc:
        log.debug("Ollama /api/ps unavailable: %s", exc)

    result["gpus"] = gpus_result
    result["loaded_models"] = [m["name"] for m in loaded_models]
    result["loaded_model_count"] = len(loaded_models)

    # Ollama model listing (separate from running models for disk display)
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
        pending_bytes = b""

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", target_url, json=body) as resp:
                    async for chunk in resp.aiter_bytes():
                        pending_bytes += chunk
                        while True:
                            event_sep = pending_bytes.find(b"\n\n")
                            if event_sep < 0:
                                break
                            event_bytes = pending_bytes[:event_sep]
                            pending_bytes = pending_bytes[event_sep + 2 :]
                            event_bytes = event_bytes.replace(b"\r\n", b"\n")
                            event_bytes = event_bytes.replace(b"\r", b"\n")

                            for line in event_bytes.split(b"\n"):
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
                                        if not content and isinstance(payload.get("message"), dict):
                                            content = payload.get("message", {}).get("content") or ""
                                        if content:
                                            token_count += 1
                                            tracker.record_token(request_id)
                                        if delta.get("tool_calls"):
                                            tool_call_seen = True
                                    usage = payload.get("usage")
                                    if usage:
                                        tracker.update_from_response(request_id, payload)
                                except (json.JSONDecodeError, KeyError, TypeError):
                                    pass

                            event_bytes = remap_reasoning_to_content(event_bytes)

                            # Inject fallback message before [DONE] when model produces no output
                            if (
                                token_count == 0
                                and not tool_call_seen
                                and b"data: [DONE]" in event_bytes
                                and not fallback_injected
                            ):
                                log.warning("[%s] empty content — injecting fallback", request_id)
                                synth = (
                                    f"data: {{\"id\":\"{saved_id}\",\"object\":\"chat.completion.chunk\","
                                    f"\"choices\":[{{\"index\":0,\"delta\":{{\"content\":\"[Model produced no output — please retry]\"}},\"finish_reason\":null}}]}}\n\n"
                                )
                                yield synth.encode()
                                fallback_injected = True

                            yield event_bytes + b"\n\n"

                    if pending_bytes:
                        event_bytes = pending_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                        for line in event_bytes.split(b"\n"):
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
                                    if not content and isinstance(payload.get("message"), dict):
                                        content = payload.get("message", {}).get("content") or ""
                                    if content:
                                        token_count += 1
                                        tracker.record_token(request_id)
                                    if delta.get("tool_calls"):
                                        tool_call_seen = True
                                usage = payload.get("usage")
                                if usage:
                                    tracker.update_from_response(request_id, payload)
                            except (json.JSONDecodeError, KeyError, TypeError):
                                pass

                        event_bytes = remap_reasoning_to_content(event_bytes)
                        yield event_bytes + b"\n\n"

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
        "version": PROXY_VERSION,
        "build_timestamp": BUILD_TIMESTAMP,
    }


# ── Azure OpenAI compatibility shims ─────────────────────────────────────────

@app.post("/openai/deployments/{deployment}/chat/completions")
@app.get("/openai/deployments/{deployment}/chat/completions")
async def azure_chat(deployment: str, request: Request):
    return await chat_completions(request)


@app.get("/openai/models")
async def azure_models():
    return await list_models()


# ── Admin: API Key Management ─────────────────────────────────────────────────

@app.get("/api/admin/keys")
async def admin_list_keys(creds: HTTPBasicCredentials = Depends(verify_admin)):
    """List all dynamic API keys (admin only). Does not expose key hashes."""
    keys = await session_db.list_api_keys()
    return JSONResponse({
        "data": keys,
        "static_key_count": len(STATIC_API_KEYS),
        "auth_enabled": API_KEY_REQUIRED,
    })


@app.post("/api/admin/keys")
async def admin_create_key(request: Request, creds: HTTPBasicCredentials = Depends(verify_admin)):
    """Generate a new API key (admin only). Returns the key once — store it securely."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    name = str(body.get("name", "unnamed"))[:64]
    key = _secrets_mod.token_urlsafe(32)
    key_id = await session_db.create_api_key(name, key)
    log.info("Admin created API key '%s' (id=%s)", name, key_id)
    return JSONResponse({
        "key_id": key_id,
        "key": key,
        "name": name,
        "note": "Store this key securely — it will not be shown again.",
    })


@app.delete("/api/admin/keys/{key_id}")
async def admin_revoke_key(key_id: str, creds: HTTPBasicCredentials = Depends(verify_admin)):
    """Revoke a dynamic API key by ID (admin only)."""
    found = await session_db.revoke_api_key(key_id)
    log.info("Admin revoked API key id=%s (found=%s)", key_id, found)
    return JSONResponse({"status": "revoked" if found else "not_found", "key_id": key_id})


@app.get("/api/admin/status")
async def admin_status(creds: HTTPBasicCredentials = Depends(verify_admin)):
    """Return auth/security configuration status (admin only)."""
    return JSONResponse({
        "api_key_required": API_KEY_REQUIRED,
        "static_key_count": len(STATIC_API_KEYS),
        "admin_configured": bool(ADMIN_USERNAME and ADMIN_PASSWORD),
        "mongodb_enabled": session_db.enabled,
    })
