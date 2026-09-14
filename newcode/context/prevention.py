from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from newcode.session import ChatMessage, ChatSession

from .artifacts import ArtifactStore
from .estimator import estimate_message
from .redaction import redact_value

SINGLE_RESULT_THRESHOLD = 8_000
MESSAGE_RESULTS_THRESHOLD = 12_000
PREVIEW_LIMIT = 1_200


@dataclass(frozen=True)
class PreventionResult:
    externalized: int
    diagnostics: tuple[str, ...]


def externalize_tool_results(session: ChatSession, store: ArtifactStore) -> PreventionResult:
    messages = list(session.messages)
    candidates = [(index, estimate_message(message)) for index, message in enumerate(messages) if message.role == "tool"]
    selected = {index for index, size in candidates if size >= SINGLE_RESULT_THRESHOLD}
    for group in _tool_groups(messages):
        total = sum(size for index, size in candidates if index in group and index not in selected)
        if total >= MESSAGE_RESULTS_THRESHOLD:
            for index, _ in sorted(((i, size) for i, size in candidates if i in group and i not in selected), key=lambda item: (-item[1], item[0])):
                selected.add(index)
                total -= next(size for candidate, size in candidates if candidate == index)
                if total < MESSAGE_RESULTS_THRESHOLD:
                    break
    diagnostics: list[str] = []
    replacements: dict[int, ChatMessage] = {}
    for index in sorted(selected):
        message = messages[index]
        try:
            payload = _tool_payload(message.content)
            safe_payload = redact_value(payload, store.sensitive_values)
            artifact = store.write(str(safe_payload.get("tool_name", "tool")), safe_payload)
            preview = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True)[:PREVIEW_LIMIT]
            replacement = {
                "context_artifact": artifact.relative_path,
                "preview": preview,
                "truncated": artifact.truncated,
                "omitted": True,
            }
            replacements[index] = ChatMessage(role="tool", content=json.dumps(replacement, ensure_ascii=False), tool_call_id=message.tool_call_id)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            diagnostics.append("tool_result_artifact_unavailable")
    if replacements:
        session.replace_messages([replacements.get(index, message) for index, message in enumerate(messages)])
    return PreventionResult(externalized=len(replacements), diagnostics=tuple(diagnostics))


def _tool_groups(messages: list[ChatMessage]) -> list[set[int]]:
    groups: list[set[int]] = []
    current: set[int] = set()
    for index, message in enumerate(messages):
        if message.role == "tool":
            current.add(index)
        elif current:
            groups.append(current)
            current = set()
    if current:
        groups.append(current)
    return groups


def _tool_payload(content: str | None) -> dict[str, Any]:
    if content is None:
        return {"content": ""}
    value = json.loads(content)
    return value if isinstance(value, dict) else {"content": value}
