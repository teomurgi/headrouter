"""Headroom compression integration.

Compresses conversation messages when the estimated token count exceeds a
threshold, using the mandatory `headroom-ai` package's TransformPipeline.

Compression NEVER blocks a request: any failure degrades to passthrough.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from headroom import HeadroomConfig, TransformPipeline
from headroom.agent_savings import get_agent_savings_profile
from headroom.config import HeadroomMode
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig

logger = logging.getLogger("headrouter.compression")

_CHARS_PER_TOKEN = 4  # rough fallback estimate
_DEFAULT_MODEL_LIMIT = 128_000

# Available COMPRESSION_STRATEGY values:
# - coding: coding-agent profile; protects exact reads/errors and compresses logs,
#   repeated context, structured output, and other safe content (recommended).
# - balanced: moderate token savings with conservative user/system protection.
# - general: general-purpose token mode for non-coding conversational workloads.
# - agent-90: aggressive profile targeting roughly 90% savings; highest fidelity risk.
# - default: legacy Headroom ContentRouter defaults used before strategy support.
COMPRESSION_STRATEGIES = frozenset({"coding", "balanced", "general", "agent-90", "default"})


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
    def __init__(
        self,
        enabled: bool = True,
        threshold_tokens: int = 0,
        strategy: str = "coding",
    ):
        if strategy not in COMPRESSION_STRATEGIES:
            raise ValueError(
                f"invalid compression strategy {strategy!r}; expected one of "
                f"{sorted(COMPRESSION_STRATEGIES)}"
            )
        self.enabled = enabled
        self.threshold_tokens = threshold_tokens
        self.strategy = strategy
        self._pipeline = None
        self._pipeline_checked = False
        self._counter = _TokenCounter()
        self._compress_user_messages = True
        self._compress_system_messages = False

    @property
    def engine_available(self) -> bool:
        return self._get_pipeline() is not None

    def _get_pipeline(self):
        if not self._pipeline_checked:
            self._pipeline_checked = True
            config = HeadroomConfig(default_mode=HeadroomMode.OPTIMIZE)
            if self.strategy == "default":
                self._pipeline = TransformPipeline(config)
            else:
                profile = get_agent_savings_profile(self.strategy)
                router_config = ContentRouterConfig(
                    enable_code_aware=profile.code_aware,
                    force_kompress_all=profile.force_kompress,
                    lossless=profile.lossless,
                    enable_cross_turn_dedup=profile.cross_turn_dedup,
                    lossless_then_lossy=profile.lossless_then_lossy,
                    min_section_tokens=profile.min_tokens_to_compress,
                    protect_recent_code=profile.protect_recent,
                    protect_analysis_context=profile.protect_analysis_context,
                    smart_crusher_max_items_after_crush=profile.max_items_after_crush,
                    smart_crusher_with_compaction=profile.smart_crusher_with_compaction,
                    protect_recent_reads_fraction=(
                        0.3 if profile.proxy_mode == "token" else 0.0
                    )
                )
                if profile.min_chars_for_block is not None:
                    router_config.min_chars_for_block_compression = profile.min_chars_for_block
                self._pipeline = TransformPipeline(
                    config,
                    transforms=[ContentRouter(router_config)],
                )
                self._compress_user_messages = profile.compress_user_messages
                self._compress_system_messages = profile.compress_system_messages
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
            pipeline_kwargs = {
                "model": model,
                "model_limit": self.threshold_tokens,
            }
            if self._compress_user_messages:
                pipeline_kwargs["compress_user_messages"] = True
            if self._compress_system_messages:
                pipeline_kwargs["compress_system_messages"] = True
            try:
                result = pipeline.apply(messages, **pipeline_kwargs)
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
