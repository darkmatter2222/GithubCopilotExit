"""
LLM Proxy — OpenAI-compatible endpoint backed by Ollama.

Responsibilities:
- Clamp temperature to >= 0.6 (Qwen3 thinking mode requires >= 0.6;
  VS Code Copilot sends 0.1 by default which breaks the model).
- Strip unsupported parameters before forwarding.
- Pass tool/function calling schemas through unchanged.
- No authentication required.
- Streams responses when the client requests streaming.
"""

import os
import json
import logging
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proxy")

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
SERVED_MODEL = os.environ.get("SERVED_MODEL_NAME", "qwen3")
MIN_TEMPERATURE = float(os.environ.get("MIN_TEMPERATURE", "0.6"))

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
