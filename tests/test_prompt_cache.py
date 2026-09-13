from __future__ import annotations

import inspect
from types import SimpleNamespace

from newcode.prompt import cache
from newcode.prompt import PromptCacheUsage, parse_prompt_cache_usage
from newcode.providers import deepseek


def test_prompt_cache_usage_is_readable_and_allows_none_fields():
    usage = PromptCacheUsage()

    assert usage.cache_hit_tokens is None
    assert usage.cache_miss_tokens is None
    assert usage.raw is None


def test_parse_prompt_cache_usage_reads_prompt_cache_hit_and_miss_tokens():
    raw = {
        "prompt_cache_hit_tokens": 12,
        "prompt_cache_miss_tokens": 34,
    }

    usage = parse_prompt_cache_usage(raw)

    assert usage == PromptCacheUsage(
        cache_hit_tokens=12,
        cache_miss_tokens=34,
        raw=raw,
    )


def test_parse_prompt_cache_usage_maps_cached_tokens_to_hit_tokens():
    raw = {"cached_tokens": 56}

    usage = parse_prompt_cache_usage(raw)

    assert usage.cache_hit_tokens == 56
    assert usage.cache_miss_tokens is None
    assert usage.raw is raw


def test_parse_prompt_cache_usage_maps_cache_read_input_tokens_to_hit_tokens():
    raw = {"cache_read_input_tokens": 78}

    usage = parse_prompt_cache_usage(raw)

    assert usage.cache_hit_tokens == 78
    assert usage.cache_miss_tokens is None
    assert usage.raw is raw


def test_parse_prompt_cache_usage_reads_object_attributes():
    raw = SimpleNamespace(
        prompt_cache_hit_tokens=3,
        prompt_cache_miss_tokens=4,
    )

    usage = parse_prompt_cache_usage(raw)

    assert usage.cache_hit_tokens == 3
    assert usage.cache_miss_tokens == 4
    assert usage.raw is raw


def test_parse_prompt_cache_usage_returns_none_without_cache_fields():
    assert parse_prompt_cache_usage({"prompt_tokens": 100}) is None
    assert parse_prompt_cache_usage(SimpleNamespace(prompt_tokens=100)) is None


def test_parse_prompt_cache_usage_ignores_invalid_field_types_without_error():
    raw = {
        "prompt_cache_hit_tokens": "12",
        "prompt_cache_miss_tokens": object(),
        "cached_tokens": False,
        "cache_read_input_tokens": ["78"],
    }

    assert parse_prompt_cache_usage(raw) is None


def test_parse_prompt_cache_usage_returns_none_for_none_input():
    assert parse_prompt_cache_usage(None) is None


def test_parse_prompt_cache_usage_ignores_attribute_errors_without_error():
    class BrokenUsage:
        @property
        def prompt_cache_hit_tokens(self):
            raise RuntimeError("broken")

        @property
        def prompt_cache_miss_tokens(self):
            raise RuntimeError("broken")

    assert parse_prompt_cache_usage(BrokenUsage()) is None


def test_prompt_cache_parser_does_not_require_provider_changes():
    source = inspect.getsource(deepseek)

    assert "PromptCacheUsage" not in source
    assert "parse_prompt_cache_usage" not in source
    assert "cache_control" not in source


def test_prompt_cache_parser_does_not_depend_on_permissions():
    source = inspect.getsource(cache)

    assert "newcode.permissions" not in source
    assert "PermissionManager" not in source
    assert "PermissionMode" not in source
