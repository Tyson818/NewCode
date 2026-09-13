from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptCacheUsage:
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    raw: object | None = None


def parse_prompt_cache_usage(usage_or_metadata: object | None) -> PromptCacheUsage | None:
    if usage_or_metadata is None:
        return None

    hit_tokens = _read_int_field(
        usage_or_metadata,
        "prompt_cache_hit_tokens",
        "cached_tokens",
        "cache_read_input_tokens",
    )
    miss_tokens = _read_int_field(usage_or_metadata, "prompt_cache_miss_tokens")

    if hit_tokens is None and miss_tokens is None:
        return None

    return PromptCacheUsage(
        cache_hit_tokens=hit_tokens,
        cache_miss_tokens=miss_tokens,
        raw=usage_or_metadata,
    )


def _read_int_field(source: object, *names: str) -> int | None:
    for name in names:
        value = _read_field(source, name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _read_field(source: object, name: str) -> Any:
    if isinstance(source, dict):
        return source.get(name)
    try:
        return getattr(source, name)
    except Exception:
        return None
