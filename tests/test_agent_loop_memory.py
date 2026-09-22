from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from newcode.agent import AgentFinalAnswer, AgentLoop, AgentLoopConfig, AgentStopped
from newcode.agent.config import StopReason
from newcode.agent.mode import AgentMode
from newcode.permissions.manager import PermissionManager
from newcode.providers.base import ProviderError, TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolCall, ToolContext, ToolResult, ToolSpec


class Provider:
    def __init__(self, batches):
        self.batches = batches
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), tools, allow_tool_calls))
        yield from self.batches[len(self.calls) - 1]


class ErrorProvider:
    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        raise ProviderError("failed")


@dataclass
class MemorySpy:
    calls: list[list] = field(default_factory=list)

    def submit(self, messages):
        self.calls.append(list(messages))
        return True


@dataclass
class WriteTool:
    calls: int = 0
    name: str = "write_file"

    @property
    def spec(self):
        return ToolSpec(self.name, "write", {"type": "object"})

    def run(self, arguments, context):
        self.calls += 1
        return ToolResult.success(self.name, {})


def _loop(provider, tmp_path: Path, memory: MemorySpy, *, registry: ToolRegistry | None = None, config=None):
    return AgentLoop(
        provider=provider,
        session=ChatSession(),
        registry=registry or ToolRegistry(),
        tool_context=ToolContext(tmp_path),
        memory_service=memory,
        permission_manager=PermissionManager(),
        config=config,
    )


def test_natural_final_answer_submits_once_after_final_event(tmp_path: Path):
    memory = MemorySpy()
    loop = _loop(Provider([[TextDelta("done")]]), tmp_path, memory)
    events = loop.run("hello")
    produced = []
    while not isinstance(produced[-1] if produced else None, AgentFinalAnswer):
        produced.append(next(events))

    assert produced[-1] == AgentFinalAnswer("done")
    assert memory.calls == []
    with pytest.raises(StopIteration):
        next(events)
    assert len(memory.calls) == 1
    assert memory.calls[0][-1].content == "done"


def test_provider_error_and_cancellation_submit_nothing(tmp_path: Path):
    memory = MemorySpy()
    error_events = list(_loop(ErrorProvider(), tmp_path, memory).run("fail"))
    cancelled_events = list(_loop(Provider([[TextDelta("never")]]), tmp_path, memory).run("cancel", cancel_flag=True))

    assert isinstance(error_events[-1], AgentStopped)
    assert error_events[-1].reason is StopReason.PROVIDER_ERROR
    assert isinstance(cancelled_events[-1], AgentStopped)
    assert memory.calls == []


def test_plan_mode_disallowed_tool_and_max_iterations_submit_nothing(tmp_path: Path):
    memory = MemorySpy()
    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    disallowed = list(_loop(Provider([[ToolCallEvent([ToolCall("call", "write_file")])]]), tmp_path, memory, registry=registry).run("plan", mode=AgentMode.PLAN))
    maxed = list(_loop(Provider([[ToolCallEvent([ToolCall("unknown", "missing")])]]), tmp_path, memory, config=AgentLoopConfig(max_iterations=1, unknown_tool_threshold=9)).run("max"))

    assert isinstance(disallowed[-1], AgentStopped)
    assert disallowed[-1].reason is StopReason.DISALLOWED_TOOL_CALL
    assert tool.calls == 0
    assert isinstance(maxed[-1], AgentStopped)
    assert maxed[-1].reason is StopReason.MAX_ITERATIONS
    assert memory.calls == []


def test_permission_denial_and_unhandled_exception_submit_nothing(tmp_path: Path):
    memory = MemorySpy()
    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    denied = list(
        _loop(
            Provider([[ToolCallEvent([ToolCall("call", "write_file")])]]),
            tmp_path,
            memory,
            registry=registry,
            config=AgentLoopConfig(tool_error_threshold=1),
        ).run("deny")
    )

    class ExplodingPromptBuilder:
        def build_messages(self, messages, context):
            raise RuntimeError("boom")

    loop = AgentLoop(
        provider=Provider([[TextDelta("never")]]),
        session=ChatSession(),
        registry=ToolRegistry(),
        tool_context=ToolContext(tmp_path),
        memory_service=memory,
        prompt_builder=ExplodingPromptBuilder(),
    )
    with pytest.raises(RuntimeError, match="boom"):
        list(loop.run("exception"))

    assert denied[-1].reason is StopReason.TOOL_ERROR_LIMIT
    assert tool.calls == 0
    assert memory.calls == []


def test_main_provider_tools_are_unchanged_when_memory_service_is_present(tmp_path: Path):
    memory = MemorySpy()
    tool = WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    provider = Provider([[TextDelta("done")]])

    list(_loop(provider, tmp_path, memory, registry=registry).run("hello"))

    assert provider.calls[0][2] is True
    assert provider.calls[0][1][0]["function"]["name"] == "write_file"
