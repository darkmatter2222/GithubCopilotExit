"""
Token throughput tracker for LLM proxy.
Tracks active requests, token counts, and computes tokens/sec in real time.
Persists completed requests to MongoDB for historical analysis.
"""

import asyncio
import threading
import time
import logging
from dataclasses import dataclass, field

log = logging.getLogger("tracker")

# ── DB handle (set by main.py at startup) ──
db_backend = None

def set_db(backend):
    """Assign the SessionDB instance so tracker can persist requests."""
    global db_backend
    db_backend = backend

@dataclass
class RequestStats:
    request_id: str
    model: str
    start_time: float = field(default_factory=time.time)
    finish_time: float = 0.0
    prompt_tokens: int = 0          # input / context tokens
    completion_tokens: int = 0      # output tokens (accumulated during stream)
    total_tokens: int = 0           # prompt + completion (total billable tokens)
    prompt_eval_count: int = 0
    prompt_eval_duration_ns: int = 0
    eval_count: int = 0             # streamed token count
    eval_duration_ns: int = 0
    first_token_time: float = 0.0   # wall-clock of first output token
    last_token_time: float = 0.0    # wall-clock of last output token
    finished: bool = False
    ttft_ms: float = 0.0            # Time to first token in milliseconds

    @property
    def avg_completion_tps(self) -> float:
        """Average output tokens/sec for this request."""
        if self.eval_duration_ns == 0:
            return 0.0
        return round(self.eval_count / (self.eval_duration_ns / 1e9), 1)

    @property
    def streaming_tps(self) -> float:
        """Real-time TPS from token arrival times, or fallback to eval_duration."""
        if self.first_token_time == 0 or self.last_token_time == 0:
            # Fallback: use Ollama's reported eval duration
            tps = self.avg_completion_tps
            # If eval_duration also unavailable (Ollama doesn't always report it),
            # estimate from wall-clock duration
            if tps == 0.0 and self.eval_count > 0 and self.finish_time > 0:
                dur = self.finish_time - self.start_time
                if dur > 0:
                    return round(self.eval_count / dur, 1)
            return tps
        elapsed = self.last_token_time - self.first_token_time
        # Minimum window: 10ms for meaningful measurement (prevents division by near-zero).
        # NOTE: this must never return float('inf') — it used to, and that value is not
        # JSON-serializable (json.dumps(..., allow_nan=False), which Starlette's
        # JSONResponse uses by default), causing intermittent 500s on /stats whenever a
        # very fast small-model completion (e.g. qwen3:4b with a short max_tokens) landed
        # in the "recent requests" window. Fall back to the eval-duration-based estimate
        # instead, which is always a finite number.
        if elapsed < 0.010:
            return self.avg_completion_tps
        return round(self.eval_count / elapsed, 1)

    tps_since_first_token = streaming_tps

    @property
    def is_active(self) -> bool:
        return not self.finished


class TokenTracker:
    """Thread-safe tracker for live token throughput across all requests."""

    MAX_HISTORY = 50          # completed requests to keep
    MAX_EVENTS = 100          # event log cap
    MAX_TPS_POINTS = 60       # time-series chart window (~2 min at 2-s poll)

    def __init__(self):
        self._lock = threading.Lock()
        self._requests: dict = {}         # id → RequestStats
        self._history: list = []          # finished requests, newest first
        self.session_start_time = time.time()
        self.success_count = 0
        self.error_count = 0
        self.total_prompt_tokens = 0      # monotonic session total (never decremented)
        self.total_completion_tokens = 0  # monotonic session total (never decremented)
        self.total_total_tokens = 0       # monotonic session total (input + output)
        self.tps_history: list = []       # [{ts, value}]
        self.events: list = []            # [{ts, level, message}]

    def _log_event(self, level: str, message: str) -> None:
        """Append event (caller must hold lock)."""
        self.events.append({
            "ts": time.time(),
            "time": time.strftime("%H:%M:%S"),
            "level": level,    # INFO | WARN | ERROR
            "message": message,
        })
        # Trim oldest when overflowing
        while len(self.events) > self.MAX_EVENTS:
            self.events.pop(0)

    # ── lifecycle ────────────────────────────────────────────────
    def new_request_id(self) -> str:
        with self._lock:
            return f"req-{len(self._requests)}"

    def start_request(self, request_id: str, model: str) -> None:
        with self._lock:
            self._requests[request_id] = RequestStats(
                request_id=request_id, model=model
            )
            self._log_event("INFO", f"{request_id} {model} started")

    def record_token(self, request_id: str) -> None:
        """Call once per output token arriving from stream."""
        now = time.time()
        with self._lock:
            s = self._requests.get(request_id)
            if not s:
                return
            s.eval_count += 1
            s.completion_tokens += 1
            s.last_token_time = now
            if s.first_token_time == 0.0:
                s.first_token_time = now
                # Calculate TTFT when first token arrives
                s.ttft_ms = (s.first_token_time - s.start_time) * 1000  # Convert to milliseconds

    def _estimate_ttft_from_eval_duration(self, s: RequestStats) -> None:
        """Estimate TTFT from eval_duration_ns if first token time unavailable."""
        if s.ttft_ms == 0.0 and s.eval_duration_ns > 0:
            total_time_s = (s.finish_time or time.time()) - s.start_time
            prompt_fraction = 0.4
            estimated_ttft_ns = s.prompt_eval_duration_ns * prompt_fraction
            if estimated_ttft_ns <= 0:
                estimated_ttft_ns = s.eval_duration_ns * prompt_fraction
            s.ttft_ms = round(estimated_ttft_ns / 1e6, 1)

    def update_from_response(self, request_id: str, response: dict) -> None:
        """Merge usage block from Ollama final chunk / non-streamed reply."""
        with self._lock:
            s = self._requests.get(request_id)
            if not s:
                return
            usage = response.get("usage", {})
            s.prompt_tokens = usage.get("prompt_tokens", 0)
            # Keep the streamed count OR the usage block count, whichever is higher
            ct = max(s.completion_tokens, usage.get("completion_tokens", 0), s.eval_count)
            s.completion_tokens = ct
            s.eval_count = max(s.eval_count, ct)
            # Compute total tokens (Ollama reports: prompt_tokens, completion_tokens, total_tokens)
            s.total_tokens = max(
                s.prompt_tokens + s.completion_tokens,
                usage.get("total_tokens", 0),
            )
            # Optional timing hints from Ollama
            s.prompt_eval_count = usage.get("prompt_eval_count", s.prompt_eval_count)
            s.prompt_eval_duration_ns = usage.get("prompt_eval_duration_ns",
                                                  s.prompt_eval_duration_ns)
            s.eval_duration_ns = usage.get("eval_duration_ns", s.eval_duration_ns)

    def record_error(self, request_id: str, message: str) -> None:
        with self._lock:
            s = self._requests.get(request_id)
            if s:
                s.finished = True
                s.finish_time = time.time()
                self._requests.pop(request_id, None)
            self.error_count += 1
            in_t = s.prompt_tokens if s else 0
            out_t = max(s.completion_tokens, s.eval_count) if s else 0
            total_t = in_t + out_t
            dur = round((time.time() - s.start_time), 1) if s else 0
            # Store in history too
            self._history.insert(0, {
                "id": request_id,
                "model": s.model if s else "?",
                "prompt_tokens": in_t,
                "completion_tokens": out_t,
                "total_tokens": total_t,
                "duration": dur,
                "active": False,
                "error": message,
            })
            # Accumulate into monotonic session totals (never decremented)
            self.total_prompt_tokens += in_t
            self.total_completion_tokens += out_t
            self.total_total_tokens += total_t
            self._log_event("ERROR", f"{request_id}: {message}")

            # ── Persist error to MongoDB (fire-and-forget) ──
            if db_backend and db_backend.enabled:
                record = {
                    "request_id": request_id,
                    "model": s.model if s else "?",
                    "prompt_tokens": in_t,
                    "completion_tokens": out_t,
                    "total_tokens": total_t,
                    "duration_secs": dur,
                    "ttft_secs": 0,
                    "tps": 0,
                    "has_error": True,
                    "error_message": message[:500],
                }
                try:
                    asyncio.create_task(db_backend.save_request(record))
                except Exception as e:
                    log.warning(f"Failed to save error request to DB: {e}")

    def finish_request(self, request_id: str) -> None:
        with self._lock:
            s = self._requests.pop(request_id, None)
            if not s:
                return
            s.finished = True
            s.finish_time = time.time()
            self.success_count += 1
            dur = s.finish_time - s.start_time
            out = max(s.completion_tokens, s.eval_count)
            in_t = s.prompt_tokens
            total_t = max(in_t + out, s.total_tokens)
            # Compute TPS — prefer streaming measurement; fall back to duration-based calc
            tps = s.streaming_tps if dur > 0 and out else 0
            if tps == 0 and dur > 0:
                tps = round(out / dur, 1)

            # Push to history
            entry = {
                "id": s.request_id,
                "model": s.model,
                "prompt_tokens": in_t,
                "completion_tokens": out,
                "total_tokens": total_t,
                "duration": round(dur, 1),
                "ttft_ms": round(s.ttft_ms, 1),
                "ttft": round(s.ttft_ms, 1),
                "tps": tps,
                "active": False,
            }
            self._history.insert(0, entry)
            while len(self._history) > self.MAX_HISTORY:
                self._history.pop()

            self._log_event("INFO",
                            f"{s.request_id} done · {in_t} in / {out} out · {dur:.1f}s")
            # Accumulate into monotonic session totals (never decremented)
            self.total_prompt_tokens += in_t
            self.total_completion_tokens += out
            self.total_total_tokens += total_t

            # ── TTFT estimation fallback before persisting to MongoDB ──
            self._estimate_ttft_from_eval_duration(s)

            # ── Persist to MongoDB (fire-and-forget, non-blocking) ──
            if db_backend and db_backend.enabled:
                record = {
                    "request_id": s.request_id,
                    "model": s.model,
                    "prompt_tokens": in_t,
                    "completion_tokens": out,
                    "total_tokens": total_t,
                    "duration_secs": round(dur, 2),
                    "ttft_ms": round(s.ttft_ms, 1),
                    "tps": tps,
                    "has_error": False,
                }
                try:
                    asyncio.create_task(db_backend.save_request(record))
                except Exception as e:
                    log.warning(f"Failed to save request to DB: {e}")

    # ── chart helper (no external lock needed, called from get_active_summary) ──
    def _snapshot_tps(self) -> None:
        now = time.time()
        active = [s for s in self._requests.values() if not s.finished and s.last_token_time > 0]
        combined = sum(s.tps_since_first_token for s in active)
        self.tps_history.append({"ts": now, "value": round(combined, 1)})
        # Slide window
        while len(self.tps_history) > self.MAX_TPS_POINTS:
            self.tps_history.pop(0)

    # ── main read endpoint ───────────────────────────────────────
    def get_active_summary(self) -> dict:
        now = time.time()
        with self._lock:
            active_list = [s for s in self._requests.values() if not s.finished]

            # Compute per-request TPS with duration fallback for each active request
            def _req_tps(req: "RequestStats") -> float:
                t = round(req.tps_since_first_token, 1) if req.tps_since_first_token else 0
                if t == 0:
                    o = max(req.completion_tokens, req.eval_count)
                    el = now - req.start_time
                    if el > 0 and o:
                        t = round(o / el, 1)
                return t

            recent = [s for s in self._requests.values()
                      if not s.finished and (now - s.last_token_time) < 10]
            combined_tps = sum(_req_tps(s) for s in recent)
            # Active requests still in flight (not yet in history)
            active_in = sum(s.prompt_tokens for s in self._requests.values())
            active_out_sum = sum(max(s.completion_tokens, s.eval_count)
                                for s in self._requests.values())
            # Session totals = monotonic accumulator + currently active requests
            total_in = self.total_prompt_tokens + active_in
            total_out = self.total_completion_tokens + active_out_sum
            total_total = self.total_total_tokens + active_in + active_out_sum

            self._snapshot_tps()

            active_summaries = []
            for s in active_list:
                out = max(s.completion_tokens, s.eval_count)
                elapsed = now - s.start_time
                entry_total = max(s.prompt_tokens + out, s.total_tokens)
                # Live TPS: prefer streaming measurement; fall back to duration-based calc
                live_tps = round(s.tps_since_first_token, 1) if s.tps_since_first_token else 0
                if live_tps == 0 and elapsed > 0 and out:
                    live_tps = round(out / elapsed, 1)
                active_summaries.append({
                    "id": s.request_id,
                    "model": s.model,
                    "prompt_tokens": s.prompt_tokens,
                    "completion_tokens": out,
                    "total_tokens": entry_total,
                    "tps": live_tps,
                    "elapsed": round(elapsed, 1),
                    "ttft": round(s.ttft_ms, 1),
                    "ttft_ms": round(s.ttft_ms, 1),
                })

            active_models = sorted({s.get("model") for s in active_summaries if s.get("model")})

            return {
                # session-level
                "session_uptime_s": round(now - self.session_start_time),
                "total_requests": len(self._history) + len(active_list),
                "success_count": self.success_count,
                "error_count": self.error_count,
                "active_requests": len(active_list),
                "active_models": active_models,
                "active_model_count": len(active_models),

                # throughput
                "combined_tps": round(combined_tps, 1),
                "total_input_tokens": total_in,
                "total_output_tokens": total_out,
                "total_total_tokens": total_total,

                # charts
                "tps_history": list(self.tps_history),
                "io_series": [h for h in self._history[:15] if h.get("prompt_tokens") or h.get("completion_tokens")],

                # event log (newest first)
                "events": list(reversed(self.events)),

                # active detail
                "active_requests_detail": active_summaries,

                # recent history
                "history": self._history[:self.MAX_HISTORY],
            }

