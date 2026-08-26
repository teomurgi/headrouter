"""Shared request-message compression for dedicated and proxied endpoints."""

from __future__ import annotations

import logging
from typing import Any

from compression_service import CompressionResult


async def compress_request_messages(
    state: Any,
    body: dict[str, Any],
    model: str,
    logger: logging.Logger,
) -> CompressionResult:
    result = await state.compression.maybe_compress(body.get("messages") or [], model)
    body["messages"] = result.messages
    state.metrics.observe_compression(
        result.tokens_before,
        result.tokens_after,
        result.applied,
    )

    compression_ratio = result.tokens_before / max(1, result.tokens_after)
    savings_percent = 100 * result.tokens_saved / max(1, result.tokens_before)
    logger.info(
        "compression result model=%s applied=%s engine=%s original_tokens=%d "
        "compressed_tokens=%d tokens_saved=%d compression_ratio=%.3f savings_pct=%.1f transforms=%s",
        model,
        result.applied,
        result.engine,
        result.tokens_before,
        result.tokens_after,
        result.tokens_saved,
        compression_ratio,
        savings_percent,
        ",".join(result.transforms_applied or []) or "-",
    )
    return result