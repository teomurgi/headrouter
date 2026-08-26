"""Minimal SSE parser: yields (event, data) tuples from a byte stream."""

from __future__ import annotations

from typing import AsyncIterator, Tuple


def _parse_frame(raw: bytes) -> Tuple[str | None, str | None]:
    event: str | None = None
    data_lines: list[str] = []
    for line in raw.split(b"\n"):
        if line.startswith(b"event:"):
            event = line[6:].strip().decode("utf-8", "replace")
        elif line.startswith(b"data:"):
            data_lines.append(line[5:].strip().decode("utf-8", "replace"))
    data = "\n".join(data_lines) if data_lines else None
    return event, data


async def sse_events(byte_iter: AsyncIterator[bytes]) -> AsyncIterator[Tuple[str | None, str]]:
    buffer = b""
    async for chunk in byte_iter:
        buffer += chunk
        # Normalize CRLF/CR line endings to LF so frames separated by "\r\n\r\n"
        # or "\r\r" (both valid per the SSE spec) are still detected below. A
        # trailing lone "\r" is held back since the next chunk may complete
        # it into "\r\n" — normalizing it early would insert a false blank line.
        held = b""
        if buffer.endswith(b"\r"):
            buffer, held = buffer[:-1], b"\r"
        buffer = buffer.replace(b"\r\n", b"\n").replace(b"\r", b"\n") + held
        while b"\n\n" in buffer:
            raw, buffer = buffer.split(b"\n\n", 1)
            event, data = _parse_frame(raw)
            if data is not None:
                yield event, data
    # flush any trailing event without a blank line
    if buffer.strip():
        event, data = _parse_frame(buffer)
        if data is not None:
            yield event, data
