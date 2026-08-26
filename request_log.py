"""Bounded metadata-only request log (INV-6, INV-7).

The ring buffer stores header-extracted metadata only — never request or
response bodies, never key values. key_name is the config's display name
for the authenticated key; entries survive key deletion (historical).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass


@dataclass
class LogEntry:
    seq: int
    ts: float
    key_name: str
    model: str
    resolved: str
    status: int
    latency_ms: float
    tokens_in: int = 0
    tokens_out: int = 0
    compressed: bool = False
    tokens_saved: int = 0


class RequestLog:
    def __init__(self, maxlen: int = 500):
        self._entries: deque[LogEntry] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    def record(
        self,
        key_name: str,
        model: str,
        resolved: str,
        status: int,
        latency_ms: float,
        tokens_in: int = 0,
        tokens_out: int = 0,
        compressed: bool = False,
        tokens_saved: int = 0,
    ) -> LogEntry:
        with self._lock:
            self._seq += 1
            entry = LogEntry(
                seq=self._seq,
                ts=time.time(),
                key_name=key_name,
                model=model,
                resolved=resolved,
                status=status,
                latency_ms=round(latency_ms, 1),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                compressed=compressed,
                tokens_saved=tokens_saved,
            )
            self._entries.append(entry)
            return entry

    def since(self, after: int = 0) -> list[dict]:
        with self._lock:
            return [asdict(e) for e in self._entries if e.seq > after]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq
