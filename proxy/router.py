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

    # ── Ollama-enriched metadata (populated during refresh) ──
    digest: str = ""                    # SHA256 blob hash — for update detection
    modified_at: str = ""               # ISO timestamp when model was pulled to disk
    parameter_size: str = ""            # e.g. "79.7B", "30.5B"
    quantization_level: str = ""        # e.g. "Q8_0", "Q4_K_M"
    family: str = ""                    # e.g. "qwen3next", "qwen3moe", "llama"
    families: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)  # completion, tools, thinking, vision
    parent_model: str = ""              # non-empty for aliases (e.g. Qwen3 wraps qwen3.6 MTP)
    format_: str = "gguf"               # model file format
    embedding_length: int = 0
    expert_count: int = 0               # for MoE models
    expert_used_count: int = 0          # active experts per forward pass
    block_count: int = 0                # transformer layers

    def as_openai_entry(self) -> dict:
        """OpenAI-compatible model listing (backward compatible)."""
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

    def as_enriched_entry(self) -> dict:
        """Full metadata entry for dashboard Models tab."""
        return {
            "id": self.model_id,
            "backend": self.backend,
            "display_name": self.display_name or self.model_id,
            "size_bytes": round(self.size_mb * 1024 * 1024),
            "size_mb": round(self.size_mb, 1),
            "context_length": self.context_length,
            "digest": self.digest,
            "modified_at": self.modified_at,
            "parameter_size": self.parameter_size,
            "quantization_level": self.quantization_level,
            "family": self.family,
            "families": self.families,
            "capabilities": self.capabilities,
            "parent_model": self.parent_model,
            "format": self.format_,
            "embedding_length": self.embedding_length,
            "expert_count": self.expert_count,
            "expert_used_count": self.expert_used_count,
            "block_count": self.block_count,
            "is_alias": bool(self.parent_model),
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
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{self._ollama_url}/api/tags")
            if r.status_code == 200:
                for m in r.json().get("models", []):
                    full_name: str = m.get("name", "")
                    if not full_name:
                        continue
                    size_mb = m.get("size", 0) / (1024 * 1024)
                    details = m.get("details", {})
                    backend = ModelBackend(
                        model_id=full_name,
                        backend="ollama",
                        base_url=self._ollama_url,
                        display_name=full_name,
                        size_mb=size_mb,
                        context_length=details.get("context_length", 131072),
                        digest=m.get("digest", ""),
                        modified_at=m.get("modified_at", ""),
                        parameter_size=details.get("parameter_size", ""),
                        quantization_level=details.get("quantization_level", ""),
                        family=details.get("family", ""),
                        families=list(details.get("families") or []),
                        capabilities=list(m.get("capabilities") or []),
                        parent_model=details.get("parent_model", ""),
                        format_=details.get("format", "gguf"),
                        embedding_length=details.get("embedding_length", 0),
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

    async def get_enriched_models(self) -> list[dict]:
        """
        Return full metadata for all discovered models.
        
        Enriches Ollama models with deep architecture data from /api/show
        (block_count, expert counts from model_info).  vLLM models return
        whatever /api/tags gave us.
        """
        async with self._lock:
            seen: set[str] = set()
            unique_models: list[ModelBackend] = []
            for m in self._models.values():
                if m.model_id not in seen:
                    seen.add(m.model_id)
                    unique_models.append(m)

        enriched: list[dict] = []
        for m in sorted(unique_models, key=lambda x: x.model_id):
            entry = m.as_enriched_entry()

            # Enrich all non-HF-URI Ollama models with /api/show deep data
            # (skip hf.co paths — those are raw registry blobs without Modelfile)
            if m.backend == "ollama" and not m.model_id.startswith("hf.co/"):
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.post(
                            f"{self._ollama_url}/api/show",
                            json={"name": m.model_id},
                        )
                    if resp.status_code == 200:
                        show = resp.json()
                        mi = show.get("model_info", {})
                        arch = m.family or ""
                        entry["block_count"]              = int(mi.get(f"{arch}.block_count", m.block_count))
                        entry["expert_count"]             = int(mi.get(f"{arch}.expert_count", m.expert_count))
                        entry["expert_used_count"]        = int(mi.get(f"{arch}.expert_used_count", m.expert_used_count))
                        entry["parameter_count_exact"]    = int(mi.get("general.parameter_count", 0))
                        entry["license_short"]            = self._extract_license(show.get("license", ""))
                except Exception as exc:
                    log.warning("Failed to enrich %s from /api/show: %s", m.model_id, exc)

            enriched.append(entry)
        return enriched

    async def check_updates(self) -> dict:
        """
        Compare local model digests against ollama.com global registry.
        
        Returns dict mapping model_id -> {status, local_digest, registry_digest, ...}
        """
        async with self._lock:
            seen: set[str] = set()
            unique_models: list[ModelBackend] = []
            for m in self._models.values():
                if m.model_id not in seen:
                    seen.add(m.model_id)
                    unique_models.append(m)

        result: dict[str, dict] = {}

        # Fetch global registry (ollama.com)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get("https://ollama.com/api/tags")
            if r.status_code != 200:
                return {"error": f"ollama.com returned HTTP {r.status_code}"}
            registry_models = {m["name"]: m for m in r.json().get("models", [])}
        except Exception as exc:
            return {"error": f"Failed to reach ollama.com: {exc}"}

        for m in unique_models:
            # Try exact match first (e.g. "qwen3-coder-next:q8_0")
            reg = registry_models.get(m.model_id)
            if not reg:
                # Try base name match (e.g. "qwen3" matches "qwen3:latest")
                base = m.model_id.split(":")[0]
                for rname, rval in registry_models.items():
                    if rname.startswith(base + ":"):
                        reg = rval
                        break

            if not reg:
                result[m.model_id] = {
                    "status": "unknown",
                    "message": "Not found in global registry (may be local/custom alias)",
                }
                continue

            if m.digest and reg.get("digest"):
                if m.digest == reg["digest"]:
                    result[m.model_id] = {
                        "status": "up_to_date",
                        "local_digest": m.digest[:16],
                        "registry_digest": reg["digest"][:16],
                        "registry_modified": reg.get("modified_at", ""),
                        "local_modified": m.modified_at,
                    }
                else:
                    result[m.model_id] = {
                        "status": "update_available",
                        "local_digest": m.digest[:16],
                        "registry_digest": reg["digest"][:16],
                        "registry_modified": reg.get("modified_at", ""),
                        "local_modified": m.modified_at,
                    }
            else:
                result[m.model_id] = {
                    "status": "unknown",
                    "message": "Digest comparison not available (model may be custom-built)",
                    "registry_modified": reg.get("modified_at", ""),
                }

        return result

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _extract_license(license_text: str) -> str:
        """Heuristic: extract short license name from full license text."""
        license_keywords = [
            ("Apache License", "Apache 2.0"),
            ("MIT License", "MIT"),
            ("GPL", "GPL"),
            ("Llama", "Meta Llama"),
            ("Mistral", "Mistral"),
        ]
        upper_text = license_text.upper() if license_text else ""
        for pattern, short in license_keywords:
            if pattern.upper() in upper_text:
                return short
        return "Unknown"

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
