"""Minimal SSE parser: yields (event, data) tuples from a byte stream."""

from __future__ import annotations

from typing import AsyncIterator, Tuple


async def sse_events(byte_iter: AsyncIterator[bytes]) -> AsyncIterator[Tuple[str | None, str]]:
    buffer = b""
    async for chunk in byte_iter:
        buffer += chunk
        while b"\n\n" in buffer:
            raw, buffer = buffer.split(b"\n\n", 1)
            event: str | None = None
            data: str | None = None
            for line in raw.split(b"\n"):
                if line.startswith(b"event:"):
                    event = line[6:].strip().decode("utf-8", "replace")
                elif line.startswith(b"data:"):
                    data = line[5:].strip().decode("utf-8", "replace")
            if data is not None:
                yield event, data
    # flush any trailing event without a blank line
    if buffer.strip():
        for line in buffer.split(b"\n"):
            if line.startswith(b"data:"):
                yield None, line[5:].strip().decode("utf-8", "replace")
