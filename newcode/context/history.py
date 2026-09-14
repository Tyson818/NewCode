from __future__ import annotations

import json
from collections.abc import Callable

from newcode.session import ChatMessage, ChatSession

from .estimator import estimate_message, estimate_messages
from .summary import build_summary_prompt, parse_summary_response

AUTO_THRESHOLD = 51_000
RECENT_TOKENS = 10_000
RECENT_MESSAGES = 5


def select_recent_start(messages: list[ChatMessage]) -> int:
    total = 0
    count = 0
    start = len(messages)
    for index in range(len(messages) - 1, -1, -1):
        total += estimate_message(messages[index])
        count += 1
        start = index
        if total >= RECENT_TOKENS and count >= RECENT_MESSAGES:
            break
    return _expand_tool_exchange(messages, start)


def compact_history(session: ChatSession, generator: Callable[[str], str]) -> bool:
    messages = list(session.messages)
    start = select_recent_start(messages)
    if start <= 0:
        return False
    history = json.dumps([{"role": item.role, "content": item.content} for item in messages[:start]], ensure_ascii=False)
    summary = parse_summary_response(generator(build_summary_prompt(history)))
    if summary is None:
        return False
    boundary = ChatMessage(role="system", content="早期工具结果已被外置或摘要；需要细节时请通过正常读取工具访问列出的路径，不得依据摘要臆测。")
    session.replace_messages([ChatMessage(role="system", content=summary), boundary, *messages[start:]])
    return True


def needs_automatic_compaction(session: ChatSession) -> bool:
    return estimate_messages(session.messages) >= AUTO_THRESHOLD


def _expand_tool_exchange(messages: list[ChatMessage], start: int) -> int:
    tool_ids = {item.tool_call_id for item in messages[start:] if item.role == "tool" and item.tool_call_id}
    if not tool_ids:
        return start
    for index in range(start - 1, -1, -1):
        calls = messages[index].tool_calls or []
        if any(call.id in tool_ids for call in calls):
            return index
    return start
