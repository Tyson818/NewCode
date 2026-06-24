from __future__ import annotations

from dataclasses import dataclass

from newcode.agent import (
    AgentFinalAnswer,
    AgentIterationStarted,
    AgentStopped,
    AgentTextDelta,
    AgentToolCallStarted,
    AgentToolError,
    AgentToolResult,
    AgentUsage,
    StreamingTurnCollector,
)
from newcode.providers.base import ProviderError, TextDelta, ToolCallEvent
from newcode.tools.types import ToolCall, ToolResult


@dataclass(frozen=True)
class FakeUsageEvent:
    usage: dict[str, int]


def test_agent_event_types_are_importable_and_readable():
    tool_call = ToolCall(id="call_1", name="read_file", arguments={"path": "README.md"})
    result = ToolResult.success("read_file", {"content": "hello"})

    events = [
        AgentTextDelta("hello"),
        AgentToolCallStarted(tool_call, iteration=1),
        AgentToolResult(tool_call, result, iteration=1),
        AgentToolError(tool_call, "failed", code="tool_failed", iteration=1),
        AgentIterationStarted(iteration=1, max_iterations=8),
        AgentFinalAnswer("done"),
        AgentStopped(reason="max_iterations", message="stopped", iteration=8),
        AgentUsage({"total_tokens": 3}),
    ]

    assert events[0].text == "hello"
    assert events[1].tool_call.name == "read_file"
    assert events[2].result.ok is True
    assert events[3].code == "tool_failed"
    assert events[4].max_iterations == 8
    assert events[5].content == "done"
    assert events[6].reason == "max_iterations"
    assert events[7].usage == {"total_tokens": 3}


def test_text_delta_is_yielded_realtime_and_collected():
    collector = StreamingTurnCollector()

    events = list(collector.consume([TextDelta("你"), TextDelta("好")]))

    assert events == [AgentTextDelta("你"), AgentTextDelta("好")]
    assert collector.result.assistant_content == "你好"
    assert collector.result.tool_calls == []


def test_tool_call_event_is_collected_without_text_output():
    tool_call = ToolCall(
        id="call_1",
        name="run_command",
        arguments={"command": "dir"},
        raw_arguments='{"command": "dir"}',
    )
    collector = StreamingTurnCollector()

    events = list(
        collector.consume(
            [
                TextDelta("before "),
                ToolCallEvent([tool_call]),
                TextDelta(" after"),
            ]
        )
    )

    assert events == [AgentTextDelta("before "), AgentTextDelta(" after")]
    assert collector.result.assistant_content == "before  after"
    assert collector.result.tool_calls == [tool_call]
    assert all("run_command" not in event.text for event in events)


def test_usage_and_provider_error_are_recorded():
    usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}

    def failing_events():
        yield TextDelta("partial")
        yield FakeUsageEvent(usage)
        raise ProviderError("provider failed")

    collector = StreamingTurnCollector()

    events = list(collector.consume(failing_events()))

    assert events == [AgentTextDelta("partial"), AgentUsage(usage)]
    assert collector.result.assistant_content == "partial"
    assert collector.result.usage == usage
    assert isinstance(collector.result.provider_error, ProviderError)
    assert collector.result.provider_error.message == "provider failed"


def test_collector_does_not_leak_dsml_when_provider_emits_tool_call_event():
    tool_call = ToolCall(id="call_1", name="run_command", arguments={"command": "dir"})
    collector = StreamingTurnCollector()

    events = list(collector.consume([ToolCallEvent([tool_call])]))

    assert events == []
    assert collector.result.tool_calls == [tool_call]
    assert "DSML" not in "".join(
        event.text for event in events if isinstance(event, AgentTextDelta)
    )
