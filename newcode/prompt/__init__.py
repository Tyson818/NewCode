from __future__ import annotations

from typing import Any

from newcode.prompt.cache import PromptCacheUsage, parse_prompt_cache_usage

__all__ = [
    "PromptBuilder",
    "PromptCacheUsage",
    "PromptBuildContext",
    "PromptEnvironment",
    "PromptModule",
    "ReminderLevel",
    "ReminderPolicy",
    "StablePrompt",
    "build_system_reminder",
    "default_optional_modules",
    "default_stable_modules",
    "parse_prompt_cache_usage",
    "reminder_level_for",
]


def __getattr__(name: str) -> Any:
    if name == "PromptBuilder":
        from newcode.prompt.builder import PromptBuilder

        return PromptBuilder

    if name in {
        "PromptModule",
        "StablePrompt",
        "default_optional_modules",
        "default_stable_modules",
    }:
        from newcode.prompt import modules

        return getattr(modules, name)

    if name in {
        "PromptBuildContext",
        "PromptEnvironment",
        "ReminderLevel",
        "ReminderPolicy",
        "build_system_reminder",
        "reminder_level_for",
    }:
        from newcode.prompt import reminder

        return getattr(reminder, name)

    raise AttributeError(f"module 'newcode.prompt' has no attribute {name!r}")
