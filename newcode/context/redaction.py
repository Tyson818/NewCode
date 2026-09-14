from __future__ import annotations

from typing import Any, Iterable

_MARKERS = ("token", "secret", "password", "credential", "authorization", "cookie", "apikey", "api_key")


def redact_text(text: str, sensitive_values: Iterable[str] = ()) -> str:
    result = text
    for value in sensitive_values:
        if value:
            result = result.replace(value, "[REDACTED]")
    return result


def redact_value(value: Any, sensitive_values: Iterable[str] = ()) -> Any:
    values = tuple(sensitive_values)
    if isinstance(value, str):
        return redact_text(value, values)
    if isinstance(value, list):
        return [redact_value(item, values) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, values) for item in value]
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _is_sensitive_key(str(key)) else redact_value(item, values)
            for key, item in value.items()
        }
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = "".join(character for character in key.lower() if character.isalnum())
    return any(marker.replace("_", "") in normalized for marker in _MARKERS)
