"""
Dynamic backend router for LLM Proxy.

Discovers available models from Ollama and (optionally) vLLM on a configurable
refresh interval. Incoming requests are routed to the correct backend based on
the model name in the request body. Unknown models fall through to Ollama, which
auto-loads them from disk on the first request.

Environment variables:
  OLLAMA_BASE_URL   URL for Ollama (default: http://localhost:11434)
  VLLM_BASE_URL     Comma-separated vLLM base URLs (optional, e.g. http://localhost:8000)
  ROUTER_REFRESH_S  Seconds between auto-refresh (default: 30)
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger("router")


@dataclass
class ModelBackend:
    model_id: str
    backend: str          # "ollama" | "vllm"
    base_url: str
    display_name: str = ""
    size_mb: float = 0.0
    context_length: int = 131072
    last_seen: float = field(default_factory=time.time)

    def as_openai_entry(self) -> dict:
        return {
            "id": self.model_id,
            "object": "model",
            "created": int(self.last_seen),
            "owned_by": "local",
            "backend": self.backend,
            "display_name": self.display_name or self.model_id,
            "size_mb": round(self.size_mb, 1),
            "context_length": self.context_length,
        }


class BackendRouter:
    """Thread-safe model registry with periodic backend discovery."""

    def __init__(self) -> None:
        self._models: dict[str, ModelBackend] = {}
        self._lock = asyncio.Lock()
        self._ollama_url: str = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        self._vllm_urls: list[str] = self._parse_vllm_urls()
        self._refresh_interval: int = int(os.environ.get("ROUTER_REFRESH_S", "30"))
        self._refresh_task: Optional[asyncio.Task] = None
        self._last_refresh: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self.refresh()
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        log.info("BackendRouter started (refresh every %ds)", self._refresh_interval)

    async def stop(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval)
            try:
                await self.refresh()
            except Exception as exc:
                log.warning("Backend refresh error: %s", exc)

    # ── Discovery ─────────────────────────────────────────────────────────

    async def refresh(self) -> None:
        """Re-query all configured backends and rebuild the model map."""
        discovered: dict[str, ModelBackend] = {}

        # ── Ollama (always primary) ──
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self._ollama_url}/api/tags")
            if r.status_code == 200:
                for m in r.json().get("models", []):
                    full_name: str = m.get("name", "")
                    if not full_name:
                        continue
                    size_mb = m.get("size", 0) / (1024 * 1024)
                    backend = ModelBackend(
                        model_id=full_name,
                        backend="ollama",
                        base_url=self._ollama_url,
                        display_name=full_name,
                        size_mb=size_mb,
                    )
                    discovered[full_name] = backend
                    # Register base name alias (e.g. "qwen3:latest" → also "qwen3")
                    base = full_name.split(":")[0]
                    if base != full_name and base not in discovered:
                        discovered[base] = backend
            else:
                log.warning("Ollama /api/tags returned HTTP %d", r.status_code)
        except Exception as exc:
            log.warning("Ollama discovery failed: %s", exc)

        # ── vLLM instances (optional) ──
        for vllm_url in self._vllm_urls:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(f"{vllm_url}/v1/models")
                if r.status_code == 200:
                    for m in r.json().get("data", []):
                        mid: str = m.get("id", "")
                        if not mid:
                            continue
                        discovered[mid] = ModelBackend(
                            model_id=mid,
                            backend="vllm",
                            base_url=vllm_url,
                            display_name=mid,
                            context_length=m.get("max_model_len", 131072),
                        )
                else:
                    log.warning("vLLM %s returned HTTP %d", vllm_url, r.status_code)
            except Exception as exc:
                log.warning("vLLM discovery failed (%s): %s", vllm_url, exc)

        async with self._lock:
            self._models = discovered
            self._last_refresh = time.time()

        log.info(
            "Backend refresh: %d model(s) discovered — %s",
            len(discovered),
            list(discovered.keys()),
        )

    # ── Routing ───────────────────────────────────────────────────────────

    async def get_backend(self, model_id: str) -> ModelBackend:
        """
        Resolve a model_id to a backend.

        Resolution order:
          1. Exact match in discovered models
          2. Prefix/substring match (e.g. "qwen3" → "qwen3:latest")
          3. Fall-through to Ollama (auto-loads from disk on first request)
        """
        async with self._lock:
            models = self._models

        if model_id in models:
            return models[model_id]

        # Prefix match
        for key, backend in models.items():
            if key.startswith(model_id) or model_id.startswith(key.split(":")[0]):
                log.debug("Model '%s' matched via prefix → '%s'", model_id, key)
                return backend

        # Fall-through: route to Ollama (it will auto-pull/load)
        log.info("Model '%s' not in registry — routing to Ollama (auto-load)", model_id)
        return ModelBackend(
            model_id=model_id,
            backend="ollama",
            base_url=self._ollama_url,
            display_name=model_id,
        )

    async def get_all_models(self) -> list[ModelBackend]:
        """Return a deduplicated list of all discovered models."""
        async with self._lock:
            seen: set[str] = set()
            out: list[ModelBackend] = []
            for m in self._models.values():
                if m.model_id not in seen:
                    seen.add(m.model_id)
                    out.append(m)
        return sorted(out, key=lambda x: x.model_id)

    async def get_ollama_running(self) -> list[dict]:
        """Return models currently loaded into Ollama VRAM (from /api/ps)."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self._ollama_url}/api/ps")
            if r.status_code == 200:
                return r.json().get("models", [])
        except Exception:
            pass
        return []

    # ── Helpers ───────────────────────────────────────────────────────────

    def _parse_vllm_urls(self) -> list[str]:
        raw = os.environ.get("VLLM_BASE_URL", "")
        if not raw:
            return []
        return [u.strip() for u in raw.split(",") if u.strip()]

    @property
    def ollama_url(self) -> str:
        return self._ollama_url

    @property
    def last_refresh_ts(self) -> float:
        return self._last_refresh
