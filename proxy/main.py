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
- Persists all request data to MongoDB for historical analysis.
"""

import os
import sys
import json
import time
import uuid
import logging
import subprocess
import asyncio
from contextlib import asynccontextmanager

# ── Load .env before anything else ───────────────────────────────────────
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proxy")

VLLM_BASE = os.environ.get("VLLM_BASE_URL", "http://localhost:8000")
VLLM_CODER_BASE = os.environ.get("VLLM_CODER_BASE_URL", "http://localhost:8002")
SERVED_MODEL = os.environ.get("SERVED_MODEL_NAME", "qwen3")
MIN_TEMPERATURE = float(os.environ.get("MIN_TEMPERATURE", "0.6"))

# ── Multi-model registry ──────────────────────────────────────────────
# Maps client-facing model aliases to actual Ollama model names.
# The proxy can serve multiple models simultaneously - clients specify which
# model they want in the request body, or use dedicated endpoints.
MODEL_REGISTRY = {
    "qwen3": {"ollama_name": "qwen3", "display_name": "Qwen3.6-27B", "default": True},
    "qwen3-coder": {"ollama_name": "qwen3-coder", "display_name": "Qwen3-Coder-Next-27B", "default": False},
    "obliterated": {"ollama_name": "obliterated", "display_name": "Qwen3.6-27B-OBLITERATED", "default": False},
}
# Allow overriding via env var (comma-separated: alias=ollama_name)
# When set, the override REPLACES the entire registry.
default_model_env = os.environ.get("MODEL_REGISTRY_OVERRIDE", "")
if default_model_env:
    MODEL_REGISTRY.clear()
    first = True
    for pair in default_model_env.split(","):
        # Support both alias:ollama_name and alias=ollama_name formats
        sep = ":" if ":" in pair else ("=" if "=" in pair else None)
        if sep:
            alias, ollama_name = pair.split(sep, 1)
            MODEL_REGISTRY[alias.strip()] = {
                "ollama_name": ollama_name.strip(),
                "display_name": ollama_name.strip(),
                "default": first,
            }
            first = False

# Get default model alias
default_model_alias = next(
    (alias for alias, cfg in MODEL_REGISTRY.items() if cfg.get("default")),
    list(MODEL_REGISTRY.keys())[0] if MODEL_REGISTRY else "qwen3"
)

# ── Ollama bug workaround (temporary) ──────────────────────────────────
# Bug: When thinking is ON and tools are present, Qwen3 puts all output into
#  tokens and produces empty content + no tool_calls in the response.
#
# Root cause in Ollama ChatHandler: req.Think == nil → forced to true for any
# model with CapabilityThinking. With thinking + tools simultaneously, the
# thinking parser strips reasoning content, leaving nothing for the tool parser.
#
# Workaround: Inject reasoning_effort="none" into requests that carry tools.
# This is the only path through Ollama's /v1 endpoint that sets Think=false.
#
# Tracking:
#   Issue:  https://github.com/ollama/ollama/issues/10976
#   PR:     https://github.com/ollama/ollama/pull/16758
#   Related:#14493, #14601
#
# REMOVEME: Once a fixed version of Ollama is released that doesn't auto-promote
# thinking for requests with tools, set DISABLE_THINKING_FOR_TOOLS=false or
# remove this block entirely.
DISABLE_THINKING_FOR_TOOLS = os.environ.get("DISABLE_THINKING_FOR_TOOLS", "true").lower() in ("true", "1", "yes")

from tracker import TokenTracker, set_db
from db import SessionDB
import cost_engine
import psutil

tracker = TokenTracker()
session_db = SessionDB()
set_db(session_db)

# ── System stats cache (refresh every 5s to avoid expensive calls) ───
_last_sys_stats: dict = {}
_last_sys_stats_ts: float = 0.0
SYS_STATS_TTL = 5.0

async def get_system_stats() -> dict:
    """Gather CPU, RAM, disk, GPU stats and Ollama model info.
    Cached for SYS_STATS_TTL seconds to avoid expensive subprocess calls."""
    global _last_sys_stats, _last_sys_stats_ts
    now = time.time()
    if _last_sys_stats and (now - _last_sys_stats_ts) < SYS_STATS_TTL:
        return _last_sys_stats

    result = {}

    # ── psutil: CPU, RAM, disk ───────────────────────────────
    try:
        cpu_pct = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        result["cpu_percent"] = round(cpu_pct, 1)
        result["ram_used_gb"] = round(mem.used / (1024**3), 1)
        result["ram_total_gb"] = round(mem.total / (1024**3), 1)
        result["ram_percent"] = mem.percent
        result["disk_used_gb"] = round(disk.used / (1024**3), 1)
        result["disk_total_gb"] = round(disk.total / (1024**3), 1)
        result["disk_percent"] = disk.percent
    except Exception as e:
        log.warning(f"psutil stats failed: {e}")

    # ── Ollama running models (DGX Spark primary data source) ──
    try:
        ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{ollama_url}/api/tags")
            if r.status_code == 200:
                models_data = r.json().get("models", [])
                result["models"] = [
                    {
                        "name": m.get("name", "?"),
                        "full_name": m.get("name", "?"),
                        "size_mb": round(m.get("size", 0) / (1024 * 1024), 1),
                        "digest": m.get("digest", ""),
                    }
                    for m in models_data
                ]
            else:
                result["models"] = []
    except Exception as e:
        log.warning(f"Ollama models request failed: {e}")
        result["models"] = []

    # ── nvidia-smi GPU stats (best effort, DGX Spark & RTX) ────
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().strip().split("\n")
        gpu_gpus = []

        def _parse_gpu_metric(raw: str) -> int:
            cleaned = raw.strip().strip("[]")
            if not cleaned or cleaned.upper() == "N/A":
                return 0
            return int(float(cleaned))

        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                try:
                    gpu_gpus.append({
                        "name": parts[0],
                        "util_percent": _parse_gpu_metric(parts[1]),
                        "mem_used_mb": _parse_gpu_metric(parts[2]),
                        "mem_total_mb": _parse_gpu_metric(parts[3]),
                        "temp_c": _parse_gpu_metric(parts[4]),
                    })
                except (ValueError, IndexError) as e:
                    log.warning(f"Failed to parse GPU data line '{line}': {e}")
                    pass
        result["gpus"] = gpu_gpus
    except Exception as e:
        # nvidia-smi not available (local Windows fallback)
        result["gpus"] = []
        log.warning(f"nvidia-smi failed: {e}")

    _last_sys_stats.clear()
    _last_sys_stats.update(result)
    _last_sys_stats_ts = time.time()
    return _last_sys_stats


@asynccontextmanager
async def lifespan(application: FastAPI):
    await session_db.ensure_connection()
    if session_db.enabled:
        log.info("MongoDB persistence enabled")
    else:
        log.warning("MongoDB not available — running memory-only (set MONGO_URI in .env)")
    yield
    await session_db.close()


app = FastAPI(title="LLM Proxy", version="2.0.0", lifespan=lifespan)

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


def resolve_model(client_model: str) -> str:
    """Resolve a client-sent model name to an actual Ollama model name.
    Returns the ollama_name from the registry, or the default if not found."""
    # Exact match in registry
    if client_model in MODEL_REGISTRY:
        return MODEL_REGISTRY[client_model]["ollama_name"]
    # Partial match (e.g. "qwen3-coder-27b" matches "qwen3-coder")
    for alias, cfg in MODEL_REGISTRY.items():
        if alias.startswith(client_model) or client_model.startswith(alias):
            return cfg["ollama_name"]
    # Fallback to default
    log.info(f"Unknown model '{client_model}', using default '{default_model_alias}'")
    return MODEL_REGISTRY.get(default_model_alias, {}).get("ollama_name", SERVED_MODEL)


def normalize_model(body: dict) -> dict:
    """Accept any model name the client sends and resolve to actual Ollama model."""
    original = body.get("model", default_model_alias)
    resolved_ollama = resolve_model(original)
    if original != resolved_ollama:
        log.info(f"Rewriting model '{original}' -> '{resolved_ollama}'")
    body["model"] = resolved_ollama
    # Store the client-facing alias for tracking/logging purposes
    body["_client_model"] = original
    return body


def suppress_thinking_for_tools(body: dict) -> dict:
    """
    Always suppress thinking unless the client explicitly requested it.

    Problem: vLLM's --default-chat-template-kwargs '{"enable_thinking": false}'
    is not reliably honored in all request paths. Qwen3 then generates heavy
    <think> chains (observed: 127 seconds for "tell me a short story"), which
    vLLM's reasoning parser strips from the stream — leaving VS Code waiting
    for first-token until it times out and shows "Sorry, no response was returned."

    Fix: inject reasoning_effort="none" for ALL requests unless:
      - reasoning_effort is already explicitly set (client wants thinking control)
      - request body contains a "thinking" key that is truthy (Anthropic-style)

    The "think" model variants (qwen3-dgx-think, qwen3-coder-next-dgx-think)
    work by VS Code sending reasoning_effort="auto"/"high" or a "thinking" param,
    which will be detected by the already_set check and left unchanged.
    """
    if not DISABLE_THINKING_FOR_TOOLS:
        return body
    already_set = body.get("reasoning_effort") is not None
    client_thinking = body.get("thinking")  # Anthropic-style {"type": "enabled"}
    explicitly_enabled = (
        client_thinking
        and isinstance(client_thinking, dict)
        and client_thinking.get("type") == "enabled"
    )
    if already_set or explicitly_enabled:
        return body
    body["reasoning_effort"] = "none"
    log.debug("Injected reasoning_effort=none (suppress thinking for all requests)")
    return body


def remap_reasoning_to_content(chunk: bytes) -> bytes:
    """Transform delta.reasoning -> delta.content in SSE chunks.

    vLLM's reasoning parser (--reasoning-parser qwen3) routes all model output
    into delta.reasoning when thinking is active. VS Code expects delta.content
    for the actual response text. Without this remap, VS Code shows everything
    in the thinking bubble and then "Sorry, no response was returned" because
    delta.content is empty.

    For nothink model variants: delta.reasoning IS the real response; remap it.
    For think variants: same behavior — thinking content becomes visible response.
    The proxy already injects reasoning_effort=none to suppress actual thinking.
    """
    lines = chunk.split(b"\n")
    new_lines = []
    changed = False
    for line in lines:
        decoded = line.strip().decode("utf-8", errors="replace")
        if decoded.startswith("data: ") and decoded != "data: [DONE]":
            data_part = decoded[len("data: "):]
            try:
                payload = json.loads(data_part)
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
    if changed:
        return b"\n".join(new_lines)
    return chunk


def prepare_body(body: dict) -> dict:
    # Defensive guard: some clients send body as pre-encoded JSON string
    if isinstance(body, str):
        import json
        try:
            body = json.loads(body)
        except Exception:
            log.warning("Failed to parse body as JSON string, returning empty request")
            return body
    body = clamp_temperature(body)
    body = normalize_model(body)
    body = suppress_thinking_for_tools(body)
    return body


@app.get("/v1/models")
async def list_models():
    """List all models served by this proxy (OpenAI-compatible format)."""
    models_data = []
    for alias, cfg in MODEL_REGISTRY.items():
        models_data.append({
            "id": alias,
            "object": "model",
            "created": 1749000000,
            "owned_by": "local",
            "display_name": cfg["display_name"],
            "ollama_name": cfg["ollama_name"],
            "capabilities": {
                "tool_calls": True,
                "vision": True,
                "thinking": True,
            },
            "context_length": 262144,
        })
    return {
        "object": "list",
        "data": models_data,
    }


# ── Dedicated child endpoints MUST be registered BEFORE parent route ────
# FastAPI processes routes in declaration order. If the parent catches first,
# it swallows child requests. Register child routes here to avoid shadowing.

@app.post("/v1/chat/completions/obliterated")
async def obliterated_completions(request: Request):
    """Dedicated endpoint for Qwen3.6-27B-OBLITERATED model.
    Equivalent to posting to /v1/chat/completions with model='obliterated'."""
    body = await request.json()
    body["model"] = "obliterated"
    body = prepare_body(body)
    body.pop("_client_model", None)

    stream = body.get("stream", False)
    target_url = f"{VLLM_BASE}/v1/chat/completions"
    client_model_alias = "obliterated"

    headers = {"Content-Type": "application/json"}

    if stream:
        request_id = tracker.new_request_id()
        tracker.start_request(request_id, client_model_alias)

        async def generate():
            token_count = 0
            thinking_token_count = 0
            tool_call_seen = False
            fallback_injected = False
            stream_had_done = False
            saved_id = "fallback"
            had_exception = False
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "POST", target_url, json=body, headers=headers
                    ) as resp:
                        async for chunk in resp.aiter_bytes():
                            for line in chunk.split(b"\n"):
                                decoded = line.strip().decode("utf-8", errors="replace")
                                if decoded.startswith("data: "):
                                    data_part = decoded[len("data: "):]
                                    if data_part == "[DONE]":
                                        stream_had_done = True
                                        continue
                                    try:
                                        payload = json.loads(data_part)
                                        saved_id = payload.get("id", saved_id)
                                        choices = payload.get("choices", [])
                                        if choices:
                                            delta = choices[0].get("delta", {})
                                            content = delta.get("content", "") or delta.get("reasoning", "")
                                            if content:
                                                token_count += 1
                                                tracker.record_token(request_id)
                                            if delta.get("tool_calls"):
                                                tool_call_seen = True
                                        usage = payload.get("usage")
                                        if usage:
                                            tracker.update_from_response(request_id, payload)
                                            thinking = usage.get("loading_tokens", 0) + usage.get("reasoning_tokens", 0)
                                            if thinking > 0:
                                                thinking_token_count = thinking
                                    except (json.JSONDecodeError, KeyError):
                                        pass
                            chunk = remap_reasoning_to_content(chunk)
                            if (token_count == 0 and not tool_call_seen
                                    and b"data: [DONE]" in chunk and not fallback_injected):
                                synth = (
                                    f'data: {{"id":"{saved_id}","object":"chat.completion.chunk",'
                                    f'"choices":[{{"index":0,"delta":{{"content":"[Model produced no output \u2014 please retry]"}},"finish_reason":null}}]}}\n\n'
                                )
                                yield synth.encode()
                                fallback_injected = True
                            yield chunk
            except Exception as exc:
                had_exception = True
                log.error(f"[{request_id}] error: {exc}")
                tracker.record_error(request_id, str(exc))
                synth = (
                    f'data: {{"id":"{saved_id}","object":"chat.completion.chunk",'
                    f'"choices":[{{"index":0,"delta":{{"content":"[Stream interrupted \u2014 please retry]"}},"finish_reason":"stop"}}]}}\n\n'
                    f'data: [DONE]\n\n'
                )
                yield synth.encode()
                return
            finally:
                tracker.finish_request(request_id)

        return StreamingResponse(generate(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(target_url, json=body, headers=headers)
        request_id = tracker.new_request_id()
        tracker.start_request(request_id, client_model_alias)
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


@app.post("/v1/chat/completions/coder")
async def coder_completions(request: Request):
    """Dedicated endpoint for Qwen3-Coder-Next model.
    Equivalent to posting to /v1/chat/completions with model='qwen3-coder'."""
    body = await request.json()
    # Force the coder model
    body["model"] = "qwen3-coder"
    body = prepare_body(body)
    # Remove internal routing field before forwarding
    body.pop("_client_model", None)

    stream = body.get("stream", False)
    target_url = f"{VLLM_CODER_BASE}/v1/chat/completions"
    client_model_alias = "qwen3-coder"

    headers = {"Content-Type": "application/json"}

    if stream:
        request_id = tracker.new_request_id()
        tracker.start_request(request_id, client_model_alias)

        async def generate():
            token_count = 0
            thinking_token_count = 0
            tool_call_seen = False
            fallback_injected = False
            stream_had_done = False
            saved_id = "fallback"
            had_exception = False
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "POST", target_url, json=body, headers=headers
                    ) as resp:
                        async for chunk in resp.aiter_bytes():
                            for line in chunk.split(b"\n"):
                                decoded = line.strip().decode("utf-8", errors="replace")
                                if decoded.startswith("data: "):
                                    data_part = decoded[len("data: "):]
                                    if data_part == "[DONE]":
                                        stream_had_done = True
                                        continue
                                    try:
                                        payload = json.loads(data_part)
                                        saved_id = payload.get("id", saved_id)
                                        choices = payload.get("choices", [])
                                        if choices:
                                            delta = choices[0].get("delta", {})
                                            # Also check delta.reasoning: vLLM routes content
                                            # there when --reasoning-parser is active
                                            content = delta.get("content", "") or delta.get("reasoning", "")
                                            if content:
                                                token_count += 1
                                                tracker.record_token(request_id)
                                            if delta.get("tool_calls"):
                                                tool_call_seen = True
                                        usage = payload.get("usage")
                                        if usage:
                                            tracker.update_from_response(request_id, payload)
                                            thinking = usage.get("loading_tokens", 0) + usage.get("reasoning_tokens", 0)
                                            if thinking > 0:
                                                thinking_token_count = thinking
                                    except (json.JSONDecodeError, KeyError):
                                        pass
                            # Remap delta.reasoning -> delta.content before forwarding
                            chunk = remap_reasoning_to_content(chunk)
                            if (token_count == 0 and not tool_call_seen
                                    and b"data: [DONE]" in chunk and not fallback_injected):
                                log.warning(
                                    f"[{request_id}] empty content detected (thinking-only?) "
                                    f"— injecting fallback message before [DONE]"
                                )
                                synth = (
                                    f'data: {{"id":"{saved_id}","object":"chat.completion.chunk",'
                                    f'"choices":[{{"index":0,"delta":{{"content":"[Model produced no output \u2014 please retry]"}},"finish_reason":null}}]}}\n\n'
                                )
                                yield synth.encode()
                                fallback_injected = True
                            yield chunk
            except Exception as exc:
                had_exception = True
                log.error(f"[{request_id}] error: {exc}")
                tracker.record_error(request_id, str(exc))
                synth = (
                    f'data: {{"id":"{saved_id}","object":"chat.completion.chunk",'
                    f'"choices":[{{"index":0,"delta":{{"content":"[Stream interrupted \u2014 please retry]"}},"finish_reason":"stop"}}]}}\n\n'
                    f'data: [DONE]\n\n'
                )
                yield synth.encode()
                return
            finally:
                tracker.finish_request(request_id)

        return StreamingResponse(generate(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(target_url, json=body, headers=headers)
        request_id = tracker.new_request_id()
        tracker.start_request(request_id, client_model_alias)
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


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    body = prepare_body(body)

    stream = body.get("stream", False)
    target_url = f"{VLLM_BASE}/v1/chat/completions"
    vllm_model = body.get("model", default_model_alias)
    client_model_alias = body.pop("_client_model", vllm_model)  # Remove internal field

    headers = {"Content-Type": "application/json"}

    # timeout=None: Qwen3 thinking-mode responses can take 10+ minutes on long
    # generations. The proxy runs on localhost so there is no network risk.
    # A hard timeout here caused ERR_INCOMPLETE_CHUNKED_ENCODING at exactly 300s.
    if stream:
        request_id = tracker.new_request_id()
        tracker.start_request(request_id, client_model_alias)

        async def generate():
            token_count = 0
            thinking_token_count = 0  # Track thinking/reasoning tokens
            tool_call_seen = False   # True when any delta.tool_calls chunk is observed
            fallback_injected = False  # Guard: only inject the fallback once
            stream_had_done = False  # True when Ollama sent data: [DONE]
            saved_id = "fallback"
            had_exception = False
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "POST", target_url, json=body, headers=headers
                    ) as resp:
                        async for chunk in resp.aiter_bytes():
                            # Parse SSE data lines to count tokens & capture usage
                            for line in chunk.split(b"\n"):
                                decoded = line.strip().decode("utf-8", errors="replace")
                                if decoded.startswith("data: "):
                                    data_part = decoded[len("data: "):]
                                    if data_part == "[DONE]":
                                        stream_had_done = True
                                        continue
                                    try:
                                        payload = json.loads(data_part)
                                        saved_id = payload.get("id", saved_id)
                                        # Count output tokens and detect tool_calls
                                        choices = payload.get("choices", [])
                                        if choices:
                                            delta = choices[0].get("delta", {})
                                            # Also check delta.reasoning: vLLM routes content
                                            # there when --reasoning-parser is active
                                            content = delta.get("content", "") or delta.get("reasoning", "")
                                            if content:
                                                token_count += 1
                                                tracker.record_token(request_id)
                                            # Track tool_calls so we don't fire the
                                            # fallback for valid tool-call-only responses
                                            if delta.get("tool_calls"):
                                                tool_call_seen = True

                                        # Capture usage block from final chunk - includes thinking tokens
                                        usage = payload.get("usage")
                                        if usage:
                                            tracker.update_from_response(request_id, payload)
                                            # Extract thinking tokens specifically
                                            thinking = usage.get("loading_tokens", 0) + usage.get("reasoning_tokens", 0)
                                            if thinking > 0:
                                                thinking_token_count = thinking
                                    except (json.JSONDecodeError, KeyError):
                                        pass
                            # Remap delta.reasoning -> delta.content before forwarding to VS Code
                            chunk = remap_reasoning_to_content(chunk)
                            # Qwen3 thinking-only response: model generated only <think>
                            # tokens (stripped by Ollama), leaving no visible content.
                            # Inject a synthetic SSE chunk before [DONE] so VS Code
                            # shows an actionable message instead of silently hanging.
                            # Skip injection if tool_calls were seen — those responses
                            # legitimately have no text content and must not get the warning.
                            if (token_count == 0 and not tool_call_seen
                                    and b"data: [DONE]" in chunk and not fallback_injected):
                                log.warning(
                                    f"[{request_id}] empty content detected (thinking-only?) "
                                    f"— injecting fallback message before [DONE]"
                                )
                                synth = (
                                    f'data: {{"id":"{saved_id}","object":"chat.completion.chunk",'
                                    f'"choices":[{{"index":0,"delta":{{"content":"[Model produced no output \u2014 please retry]"}},"finish_reason":null}}]}}\n\n'
                                )
                                yield synth.encode()
                                fallback_injected = True
                            yield chunk
            except Exception as exc:
                had_exception = True
                log.error(f"[{request_id}] error: {exc}")
                tracker.record_error(request_id, str(exc))
                # Yield a synthetic chunk so VS Code shows an error message
                # instead of the silent "No response returned" dialog.
                synth = (
                    f'data: {{"id":"{saved_id}","object":"chat.completion.chunk",'
                    f'"choices":[{{"index":0,"delta":{{"content":"[Stream interrupted \u2014 please retry]"}},"finish_reason":"stop"}}]}}\n\n'
                    f'data: [DONE]\n\n'
                )
                yield synth.encode()
                return
            finally:
                if token_count > 0 or request_id in tracker._requests:
                    log.info(
                        f"[{request_id}] completed — {token_count} tokens generated"
                    )
                    tracker.finish_request(request_id)

            # Post-loop guard: if Ollama closed the stream cleanly without sending
            # [DONE] (e.g. OOM, context overflow) and no content/tool_calls arrived,
            # VS Code would receive an empty truncated stream and silently stop.
            # Inject a synthetic + [DONE] to ensure VS Code always gets a clean close.
            if (not had_exception and not stream_had_done
                    and token_count == 0 and not tool_call_seen and not fallback_injected):
                log.warning(
                    f"[{request_id}] stream ended without [DONE] and no output "
                    f"— injecting fallback to prevent silent stop"
                )
                synth = (
                    f'data: {{"id":"{saved_id}","object":"chat.completion.chunk",'
                    f'"choices":[{{"index":0,"delta":{{"content":"[Model produced no output \u2014 please retry]"}},"finish_reason":"stop"}}]}}\n\n'
                    f'data: [DONE]\n\n'
                )
                yield synth.encode()

        return StreamingResponse(generate(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(target_url, json=body, headers=headers)
        # Capture usage from non-streamed chat response too
        request_id = tracker.new_request_id()
        tracker.start_request(request_id, client_model_alias)
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


@app.post("/v1/completions")
async def completions(request: Request):
    body = await request.json()
    body = prepare_body(body)
    stream = body.get("stream", False)
    target_url = f"{VLLM_BASE}/v1/completions"
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
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.post(
            f"{VLLM_BASE}/v1/embeddings",
            json=body,
            headers={"Content-Type": "application/json"},
        )
    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")


# ── Historical API (MongoDB-backed) ───────────────────────────────────

@app.get("/api/history")
async def get_history(days: int = 30, limit: int = 200):
    """Request history from MongoDB for the last N days."""
    docs = await session_db.get_requests(limit=limit, days=days)
    return JSONResponse(content={"count": len(docs), "data": docs})


@app.get("/api/usage/daily")
async def get_daily_usage(days: int = 30):
    """Aggregated daily token usage from MongoDB."""
    rows = await session_db.get_token_usage_by_day(days=days)
    return JSONResponse(content={"count": len(rows), "data": rows})


@app.get("/api/usage/hourly")
async def get_hourly_usage(days: int = 7):
    """Aggregated hourly token usage from MongoDB."""
    rows = await session_db.get_token_usage_by_hour(days=days)
    return JSONResponse(content={"count": len(rows), "data": rows})


@app.get("/api/stats/summary")
async def get_stats_summary(days: int = 30):
    """Summary statistics from MongoDB over the last N days."""
    data = await session_db.get_stats_summary(days=days)
    return JSONResponse(content=data)


# ── Cost analysis API ─────────────────────────────────────────────────

@app.get("/api/cost/models")
async def get_cost_models():
    """Return all cloud model pricing references."""
    return JSONResponse(content={"data": cost_engine.format_pricing_table()})


@app.get("/api/cost/summary")
async def get_cost_summary(days: int = 30):
    """Aggregate cost analysis over the last N days."""
    raw = await session_db.get_cost_summary(days=days)
    if not raw:
        return JSONResponse(content={})
    input_tokens = raw.get("total_input_tokens", 0) or 0
    output_tokens = raw.get("total_output_tokens", 0) or 0
    duration_secs = raw.get("total_duration_secs", 0) or 0

    # Compute cloud costs per tier and per model
    tier_costs = cost_engine.get_tier_summary(input_tokens, output_tokens)
    local_cost = cost_engine.get_gpu_cost_for_duration(duration_secs)

    return JSONResponse(content={
        "days": days,
        "total_requests": raw.get("total_requests", 0),
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "total_duration_hours": round(duration_secs / 3600, 2),
        "cloud_costs": {
            tier: cost_engine.dollar_fmt(v) for tier, v in tier_costs.items()
        },
        "cloud_costs_raw": tier_costs,
        "per_model_costs_raw": {
            mid: cost_engine.calculate_cloud_cost(input_tokens, output_tokens, mid)
            for mid in cost_engine.MODEL_PRICING
        },
        "local_gpu_cost": round(local_cost, 4),
        "local_gpu_cost_fmt": cost_engine.dollar_fmt(local_cost),
        "gpu_hourly_rate": round(cost_engine.get_gpu_hourly_cost(), 4),
        "savings_vs_high": round(tier_costs["high"] - local_cost, 4) if tier_costs["high"] > local_cost else 0,
        "savings_vs_medium": round(tier_costs["medium"] - local_cost, 4) if tier_costs["medium"] > local_cost else 0,
    })


@app.get("/api/cost/daily")
async def get_cost_daily(days: int = 30):
    """Daily cost breakdown for charting."""
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
    return JSONResponse(content={"count": len(enriched), "data": enriched})


# ── Live stats ─────────────────────────────────────────────────────────

@app.get("/stats")
async def stats_json():
    """Real-time token throughput stats (machine-readable JSON)."""
    data = tracker.get_active_summary()
    # Inject live session cost estimates
    total_in = data.get("total_input_tokens", 0) or 0
    total_out = data.get("total_output_tokens", 0) or 0
    tier_costs = cost_engine.get_tier_summary(total_in, total_out)
    data["live_session_cost"] = {
        "high": round(tier_costs["high"], 4),
        "medium": round(tier_costs["medium"], 4),
        "low": round(tier_costs["low"], 4),
    }
    # Inject system resource stats (cached, refreshed every 5s)
    try:
        sys_stats = await get_system_stats()
        data["sys"] = sys_stats
    except Exception as e:
        log.warning(f"System stats failed: {e}")
        data["sys"] = {}
    return data


@app.get("/dashboard", response_class=HTMLResponse)
async def stats_dashboard():
    """Comprehensive command-center dashboard — live charts, event log, history."""
    try:
        d = os.path.dirname(__file__)
        with open(os.path.join(d, "dashboard.html"), encoding="utf-8") as fhtml:
            return fhtml.read()
    except FileNotFoundError:
        # Fallback: tiny page telling user to restart
        return HTMLResponse('<h1>Dashboard template not found</h1><p>Restart proxy to load new dashboard.</p>')


@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{VLLM_BASE}/health")
            vllm_ok = r.status_code == 200
    except Exception:
        vllm_ok = False
    return {"status": "ok" if vllm_ok else "degraded", "vllm": vllm_ok}


# Azure OpenAI compatibility shims
@app.get("/openai/deployments/{deployment}/chat/completions")
@app.post("/openai/deployments/{deployment}/chat/completions")
async def azure_chat(deployment: str, request: Request):
    return await chat_completions(request)


@app.get("/openai/models")
async def azure_models():
    return await list_models()
