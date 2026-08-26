"""In-memory metrics: request counts, latency, compression and token usage."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


def _escape_label(value: str) -> str:
    """Escape a Prometheus label value (backslash, double quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass
class Metrics:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    requests: dict[tuple[str, str], int] = field(default_factory=dict)
    latency_count: int = 0
    latency_sum: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_saved: int = 0
    compression_attempts: int = 0
    compressions_applied: int = 0

    def observe_request(self, provider: str, status: int, latency: float) -> None:
        with self._lock:
            key = (provider, str(status))
            self.requests[key] = self.requests.get(key, 0) + 1
            self.latency_count += 1
            self.latency_sum += latency

    def observe_usage(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens

    def observe_compression(self, tokens_before: int, tokens_after: int, applied: bool) -> None:
        with self._lock:
            self.compression_attempts += 1
            if applied:
                self.compressions_applied += 1
                self.tokens_saved += max(0, tokens_before - tokens_after)

    def snapshot(self) -> dict:
        with self._lock:
            count = self.latency_count
            return {
                "requests_total": sum(self.requests.values()),
                "requests_by_provider_status": {
                    f"{provider}:{status}": n for (provider, status), n in sorted(self.requests.items())
                },
                "latency_avg_seconds": (self.latency_sum / count) if count else 0.0,
                "input_tokens_total": self.input_tokens,
                "output_tokens_total": self.output_tokens,
                "tokens_saved_total": self.tokens_saved,
                "compression_attempts": self.compression_attempts,
                "compressions_applied": self.compressions_applied,
            }

    def prometheus(self) -> str:
        snap = self.snapshot()
        lines = [
            "# HELP gateway_requests_total Total chat completion requests.",
            "# TYPE gateway_requests_total counter",
        ]
        for key, n in snap["requests_by_provider_status"].items():
            provider, status = key.rsplit(":", 1)
            lines.append(
                f'gateway_requests_total{{provider="{_escape_label(provider)}",'
                f'status="{_escape_label(status)}"}} {n}'
            )
        lines += [
            "# HELP gateway_request_latency_seconds Average request latency.",
            "# TYPE gateway_request_latency_seconds gauge",
            f"gateway_request_latency_seconds {snap['latency_avg_seconds']:.6f}",
            "# HELP gateway_input_tokens_total Total input tokens received by providers.",
            "# TYPE gateway_input_tokens_total counter",
            f"gateway_input_tokens_total {snap['input_tokens_total']}",
            "# HELP gateway_output_tokens_total Total output tokens returned by providers.",
            "# TYPE gateway_output_tokens_total counter",
            f"gateway_output_tokens_total {snap['output_tokens_total']}",
            "# HELP gateway_compression_tokens_saved_total Tokens removed by headroom compression.",
            "# TYPE gateway_compression_tokens_saved_total counter",
            f"gateway_compression_tokens_saved_total {snap['tokens_saved_total']}",
            "# HELP gateway_compressions_applied_total Requests where compression was applied.",
            "# TYPE gateway_compressions_applied_total counter",
            f"gateway_compressions_applied_total {snap['compressions_applied']}",
            "# HELP gateway_compression_attempts_total Requests evaluated for compression.",
            "# TYPE gateway_compression_attempts_total counter",
            f"gateway_compression_attempts_total {snap['compression_attempts']}",
        ]
        return "\n".join(lines) + "\n"


class Timer:
    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self.elapsed = time.perf_counter() - self.start
