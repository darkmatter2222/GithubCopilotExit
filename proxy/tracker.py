"""
Token throughput tracker for LLM proxy.
Tracks active requests, token counts, and computes tokens/sec in real time.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class RequestStats:
    request_id: str
    model: str
    start_time: float = field(default_factory=time.time)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_eval_count: int = 0
    prompt_eval_duration_ns: int = 0
    eval_count: int = 0
    eval_duration_ns: int = 0
    first_token_time: float = 0.0
    last_token_time: float = 0.0
    finished: bool = False

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

    def __init__(self):
        self._lock = threading.Lock()
        self._requests: Dict[str, RequestStats] = {}
        self._request_counter = 0

    def new_request_id(self) -> str:
        with self._lock:
            self._request_counter += 1
            rid = f"req-{self._request_counter}"
        return rid

    def record_token(self, request_id: str) -> None:
        """Call on each streamed token to update live stats."""
        now = time.time()
        with self._lock:
            stats = self._requests.get(request_id)
            if not stats:
                return
            stats.eval_count += 1
            stats.last_token_time = now
            if stats.first_token_time == 0.0:
                stats.first_token_time = now

    def update_from_response(self, request_id: str, response: dict) -> None:
        """Parse usage stats from Ollama's non-streamed response."""
        with self._lock:
            stats = self._requests.get(request_id)
            if not stats:
                return
            usage = response.get("usage", {})
            stats.prompt_tokens = usage.get("prompt_tokens", 0)
            stats.completion_tokens = usage.get("completion_tokens", 0)
            # Ollama may also send these fields directly in the response
            stats.eval_count = max(stats.eval_count, stats.completion_tokens, 
                                   response.get("eval_count", 0))
            stats.eval_duration_ns = response.get("eval_duration", 0)
            stats.prompt_eval_count = response.get("prompt_eval_count", 0)
            stats.prompt_eval_duration_ns = response.get(
                "prompt_eval_duration", 0)

    def finish_request(self, request_id: str) -> None:
        with self._lock:
            stats = self._requests.get(request_id)
            if stats:
                stats.finished = True

    def start_request(self, request_id: str, model: str) -> None:
        with self._lock:
            self._requests[request_id] = RequestStats(
                request_id=request_id, model=model
            )

    def get_active_summary(self) -> dict:
        """Return real-time summary of all active requests and totals."""
        now = time.time()
        with self._lock:
            active = [s for s in self._requests.values() if not s.finished]
            # Only count TPS from requests that had a token in the last 10 seconds
            recent = [s for s in self._requests.values()
                      if not s.finished and (now - s.last_token_time) < 10]

            combined_tps = sum(s.tps_since_first_token for s in recent)

            total_tokens = sum(s.eval_count for s in self._requests.values())

            return {
                "active_requests": len(active),
                "combined_tps": round(combined_tps, 1),
                "total_tokens_generated": total_tokens,
                "requests": [
                    {
                        "id": s.request_id,
                        "model": s.model,
                        "tokens_out": s.eval_count,
                        "tps": round(s.tps_since_first_token, 1),
                        "active": not s.finished,
                    }
                    for s in reversed(list(self._requests.values())[-20:])
                ],
            }

