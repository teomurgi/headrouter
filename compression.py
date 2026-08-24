"""Headroom compression integration.

Compresses conversation messages when the estimated token count exceeds a
threshold, using the `headroom-ai` package's TransformPipeline. Falls back to
token estimation only when headroom is not installed.

Compression NEVER blocks a request: any failure degrades to passthrough.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("headroom-gateway.compression")

_CHARS_PER_TOKEN = 4  # rough fallback estimate
_DEFAULT_MODEL_LIMIT = 128_000


@dataclass
class CompressionResult:
    messages: list[dict]
    applied: bool
    tokens_before: int
    tokens_after: int
    engine: str = "none"
    transforms_applied: list[str] | None = None

    @property
    def tokens_saved(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)


def _rough_estimate(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        total += len(json.dumps(msg, default=str)) // _CHARS_PER_TOKEN
    return total


class _TokenCounter:
    """Uses headroom's provider token counter when available, else chars/4."""

    def __init__(self) -> None:
        self._provider = None
        self._counters: dict[str, object] = {}
        self._checked = False

    def count(self, messages: list[dict], model: str) -> int:
        if not self._checked:
            self._checked = True
            try:
                from headroom import OpenAIProvider

                self._provider = OpenAIProvider()
            except Exception:
                self._provider = None
        if self._provider is not None:
            counter = self._counters.get(model)
            if counter is None:
                try:
                    counter = self._provider.get_token_counter(model)
                    self._counters[model] = counter
                except Exception:
                    self._counters[model] = False
                    counter = False
            if counter:
                try:
                    return int(counter.count_messages(messages))
                except Exception:  # pragma: no cover - defensive
                    pass
        return _rough_estimate(messages)


class CompressionService:
    def __init__(self, enabled: bool = True, threshold_tokens: int = 4000):
        self.enabled = enabled
        self.threshold_tokens = threshold_tokens
        self._pipeline = None
        self._pipeline_checked = False
        self._counter = _TokenCounter()

    @property
    def engine_available(self) -> bool:
        return self._get_pipeline() is not None

    def _get_pipeline(self):
        if not self._pipeline_checked:
            self._pipeline_checked = True
            try:
                from headroom import HeadroomConfig, TransformPipeline

                self._pipeline = TransformPipeline(HeadroomConfig())
            except Exception as exc:
                logger.info("headroom-ai not available, compression disabled: %s", exc)
                self._pipeline = None
        return self._pipeline

    def _count(self, messages: list[dict], model: str) -> int:
        return self._counter.count(messages, model)

    def maybe_compress(self, messages: list[dict], model: str = "gpt-4o") -> CompressionResult:
        tokens_before = self._count(messages, model)
        if not self.enabled or not messages:
            return CompressionResult(messages, False, tokens_before, tokens_before)

        if tokens_before <= self.threshold_tokens:
            return CompressionResult(messages, False, tokens_before, tokens_before)

        pipeline = self._get_pipeline()
        if pipeline is None:
            return CompressionResult(messages, False, tokens_before, tokens_before)

        try:
            # model_limit expresses the budget we want the context to fit in.
            try:
                result = pipeline.apply(
                    messages,
                    model=model,
                    model_limit=self.threshold_tokens,
                    compress_user_messages=True,
                )
            except TypeError:
                result = pipeline.apply(messages, model=model, model_limit=self.threshold_tokens)
            compressed = getattr(result, "messages", None)
            if not isinstance(compressed, list):
                return CompressionResult(messages, False, tokens_before, tokens_before)
            tokens_after = int(getattr(result, "tokens_after", 0) or 0) or self._count(compressed, model)
            applied = tokens_after < tokens_before
            transforms = list(getattr(result, "transforms_applied", []) or [])
            return CompressionResult(
                compressed if applied else messages,
                applied,
                tokens_before,
                min(tokens_after, tokens_before) if applied else tokens_before,
                engine="headroom" if applied else "none",
                transforms_applied=transforms,
            )
        except Exception as exc:
            logger.warning("headroom compression failed, passing through: %s", exc)
            return CompressionResult(messages, False, tokens_before, tokens_before)
