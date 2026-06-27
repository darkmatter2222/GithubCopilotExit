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
    thinking_tokens: int = 0        # reasoning/thinking tokens (from Ollama usage)
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
        return self.eval_count / (self.eval_duration_ns / 1e9)

    @property
    def tps_since_first_token(self) -> float:
        """Real-time tokens/sec based on token arrival times."""
        if self.first_token_time == 0 or self.last_token_time == 0:
            return 0.0
        elapsed = self.last_token_time - self.first_token_time
        if elapsed == 0:
            return 0.0
        return self.eval_count / elapsed

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
        self.total_thinking_tokens = 0    # monotonic session total for thinking tokens
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
            # Capture thinking tokens (reasoning tokens stripped by Ollama but reported in usage)
            s.thinking_tokens = usage.get("loading_tokens", 0) + usage.get("reasoning_tokens", 0)
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
            self.error_count += 1
            in_t = s.prompt_tokens if s else 0
            out_t = max(s.completion_tokens, s.eval_count) if s else 0
            think_t = s.thinking_tokens if s else 0
            dur = round((time.time() - s.start_time), 1) if s else 0
            # Store in history too
            self._history.insert(0, {
                "id": request_id,
                "model": s.model if s else "?",
                "prompt_tokens": in_t,
                "output_tokens": out_t,
                "thinking_tokens": think_t,
                "duration": dur,
                "active": False,
                "error": message,
            })
            # Accumulate into monotonic session totals (never decremented)
            self.total_prompt_tokens += in_t
            self.total_completion_tokens += out_t
            self.total_thinking_tokens += think_t
            self._log_event("ERROR", f"{request_id}: {message}")

            # ── Persist error to MongoDB (fire-and-forget) ──
            if db_backend and db_backend.enabled:
                record = {
                    "request_id": request_id,
                    "model": s.model if s else "?",
                    "prompt_tokens": in_t,
                    "completion_tokens": out_t,
                    "thinking_tokens": think_t,
                    "total_tokens": in_t + out_t + think_t,
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
            think_t = s.thinking_tokens
            # Push to history
            entry = {
                "id": s.request_id,
                "model": s.model,
                "prompt_tokens": in_t,
                "output_tokens": out,
                "thinking_tokens": think_t,
                "total_tokens": in_t + out + think_t,
                "duration": round(dur, 1),
                "ttft": round(s.first_token_time - s.start_time, 2) if s.first_token_time else 0,
                "tps": round(s.tps_since_first_token, 1),
                "active": False,
            }
            self._history.insert(0, entry)
            while len(self._history) > self.MAX_HISTORY:
                self._history.pop()

            self._log_event("INFO",
                            f"{s.request_id} done · {in_t} in / {out} out / {think_t} thinking · {dur:.1f}s")
            # Accumulate into monotonic session totals (never decremented)
            self.total_prompt_tokens += in_t
            self.total_completion_tokens += out
            self.total_thinking_tokens += think_t

            # ── Persist to MongoDB (fire-and-forget, non-blocking) ──
            if db_backend and db_backend.enabled:
                record = {
                    "request_id": s.request_id,
                    "model": s.model,
                    "prompt_tokens": in_t,
                    "completion_tokens": out,
                    "thinking_tokens": think_t,
                    "total_tokens": in_t + out + think_t,
                    "duration_secs": round(dur, 2),
                    "ttft_secs": round(s.first_token_time - s.start_time, 3) if s.first_token_time else 0,
                    "tps": round(s.tps_since_first_token, 1),
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
            recent = [s for s in self._requests.values()
                      if not s.finished and (now - s.last_token_time) < 10]

            combined_tps = sum(s.tps_since_first_token for s in recent)
            # Active requests still in flight (not yet in history)
            active_in = sum(s.prompt_tokens for s in self._requests.values())
            active_out_sum = sum(max(s.completion_tokens, s.eval_count)
                                for s in self._requests.values())
            active_think_sum = sum(s.thinking_tokens for s in self._requests.values())
            # Session totals = monotonic accumulator + currently active requests
            total_in = self.total_prompt_tokens + active_in
            total_out = self.total_completion_tokens + active_out_sum
            total_think = self.total_thinking_tokens + active_think_sum

            self._snapshot_tps()

            active_summaries = []
            for s in active_list:
                out = max(s.completion_tokens, s.eval_count)
                elapsed = now - s.start_time
                active_summaries.append({
                    "id": s.request_id,
                    "model": s.model,
                    "prompt_tokens": s.prompt_tokens,
                    "output_tokens": out,
                    "thinking_tokens": s.thinking_tokens,
                    "tps": round(s.tps_since_first_token, 1),
                    "elapsed": round(elapsed, 1),
                    "ttft": round(s.ttft_ms, 1),
                })

            return {
                # session-level
                "session_uptime_s": round(now - self.session_start_time),
                "total_requests": len(self._history) + len(active_list),
                "success_count": self.success_count,
                "error_count": self.error_count,
                "active_requests": len(active_list),

                # throughput
                "combined_tps": round(combined_tps, 1),
                "total_input_tokens": total_in,
                "total_output_tokens": total_out,
                "total_thinking_tokens": total_think,

                # charts
                "tps_history": list(self.tps_history),
                "io_series": [h for h in self._history[:15] if h.get("prompt_tokens") or h.get("output_tokens")],

                # event log (newest first)
                "events": list(reversed(self.events)),

                # active detail
                "active_requests_detail": active_summaries,

                # recent history
                "history": self._history[:self.MAX_HISTORY],
            }

