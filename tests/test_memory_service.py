from __future__ import annotations

import json
from pathlib import Path
from threading import Event
import time

import pytest

from newcode.memory.service import (
    MAX_SNAPSHOT_CHARACTERS,
    MemoryGenerationRequest,
    MemoryService,
    build_memory_snapshot,
    parse_memory_decision,
)
from newcode.memory.store import MemoryStore
from newcode.session import ChatMessage


def _store(tmp_path: Path) -> MemoryStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return MemoryStore(workspace, user_root=tmp_path / "home" / ".newcode" / "memory" / "user", sensitive_values=("secret-value",))


def _messages() -> list[ChatMessage]:
    return [
        ChatMessage("user", "用户偏好 secret-value"),
        ChatMessage("tool", '{"authorization":"secret-value","content":"tool output"}', tool_call_id="call-1"),
        ChatMessage("assistant", "已确认。"),
    ]


def _create_response() -> str:
    return json.dumps(
        {
            "action": "create",
            "scope": "user",
            "category": "用户偏好",
            "content": "偏好简洁输出",
            "tags": ["style"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _wait(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_snapshot_is_bounded_redacted_and_excludes_tool_results():
    snapshot = build_memory_snapshot(_messages(), ("secret-value",))
    long_snapshot = build_memory_snapshot([ChatMessage("user", "x" * (MAX_SNAPSHOT_CHARACTERS + 100))])

    assert "secret-value" not in snapshot
    assert "tool output" not in snapshot
    assert "authorization" not in snapshot
    assert "user:" in snapshot and "assistant:" in snapshot
    assert len(long_snapshot) <= MAX_SNAPSHOT_CHARACTERS


def test_service_makes_one_zero_tool_request_and_applies_llm_decision(tmp_path: Path):
    requests: list[MemoryGenerationRequest] = []
    service = MemoryService(_store(tmp_path), lambda request: requests.append(request) or _create_response())

    assert service.submit(_messages()) is True
    assert _wait(lambda: len(service.store.select_for_prompt()) == 1)
    service.shutdown()

    assert len(requests) == 1
    assert requests[0].tools == ()
    assert requests[0].allow_tool_calls is False
    assert requests[0].allow_file_reads is False
    assert len(service.store.select_for_prompt()) == 1


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        "```json\n{}\n```",
        '{"action":"ignore","extra":true}',
        '{"action":"create","scope":"user"}',
        '{"action":"unknown"}',
    ],
)
def test_invalid_generation_response_writes_nothing(tmp_path: Path, response: str):
    service = MemoryService(_store(tmp_path), lambda request: response)

    assert service.submit(_messages())
    assert _wait(lambda: not service._queue.unfinished_tasks)
    service.shutdown()

    assert service.store.select_for_prompt() == ()


def test_generator_exception_writes_nothing(tmp_path: Path):
    def fail(request):
        raise RuntimeError("remote secret-value failure")

    service = MemoryService(_store(tmp_path), fail)
    assert service.submit(_messages())
    assert _wait(lambda: not service._queue.unfinished_tasks)
    service.shutdown()

    assert service.store.select_for_prompt() == ()


def test_submit_is_nonblocking_and_shutdown_does_not_wait_for_running_task(tmp_path: Path):
    started = Event()
    release = Event()

    def slow(request):
        started.set()
        release.wait(2)
        return '{"action":"ignore"}'

    service = MemoryService(_store(tmp_path), slow, queue_size=1)
    began = time.monotonic()
    assert service.submit(_messages())
    assert time.monotonic() - began < 0.1
    assert started.wait(1)
    assert service.shutdown(timeout_seconds=0.01) is False
    assert service.submit(_messages()) is False
    release.set()
    assert _wait(lambda: not service._thread.is_alive())


def test_single_worker_processes_queued_requests_serially(tmp_path: Path):
    started: list[int] = []
    release = Event()

    def serial(request):
        started.append(len(started) + 1)
        if len(started) == 1:
            release.wait(1)
        return '{"action":"ignore"}'

    service = MemoryService(_store(tmp_path), serial, queue_size=2)
    assert service.submit(_messages())
    assert service.submit(_messages())
    assert _wait(lambda: started == [1])
    release.set()
    assert _wait(lambda: started == [1, 2])
    service.shutdown()


def test_decision_parser_requires_exact_json_envelope():
    assert parse_memory_decision('{"action":"ignore"}').action == "ignore"
    with pytest.raises(ValueError):
        parse_memory_decision(' {"action":"ignore"}')
