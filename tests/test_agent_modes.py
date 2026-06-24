from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from newcode.agent.config import AgentLoopConfig, StopReason
from newcode.agent.events import AgentStopped, AgentToolError
from newcode.agent.loop import AgentLoop
from newcode.agent.mode import AgentMode, allowed_tool_names, is_tool_allowed
from newcode.providers.base import ProviderEvent, TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.tools.registry import ToolRegistry, create_default_registry
from newcode.tools.types import JsonObject, ToolCall, ToolContext, ToolResult, ToolSpec


class FakeProvider:
    def __init__(self, batches: list[Iterable[ProviderEvent]]):
        self.batches = batches
        self.calls: list[dict[str, object]] = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "allow_tool_calls": allow_tool_calls,
            }
        )
        yield from self.batches[len(self.calls) - 1]


@dataclass
class FakeTool:
    name: str
    calls: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=f"{self.name} fake tool",
            parameters={"type": "object", "properties": {}},
        )

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult.success(self.name, arguments)


def tool_call(name: str, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments={})


def tool_schema_names(tools: list[dict[str, object]]) -> set[str]:
    return {tool["function"]["name"] for tool in tools}


def registry_with(*tools: FakeTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def stopped_events(events):
    return [event for event in events if isinstance(event, AgentStopped)]


def tool_errors(events):
    return [event for event in events if isinstance(event, AgentToolError)]


def test_allowed_tool_names_for_plan_and_do_modes():
    assert allowed_tool_names(AgentMode.PLAN) == {
        "read_file",
        "find_files",
        "search_code",
    }
    assert allowed_tool_names(AgentMode.DO) == {
        "read_file",
        "write_file",
        "replace_in_file",
        "run_command",
        "find_files",
        "search_code",
    }
    assert is_tool_allowed("read_file", AgentMode.PLAN) is True
    assert is_tool_allowed("write_file", AgentMode.PLAN) is False


def test_plan_mode_tools_schema_only_contains_read_only_tools():
    registry = create_default_registry()

    tools = registry.to_openai_tools(allowed_tool_names(AgentMode.PLAN))

    assert tool_schema_names(tools) == {"read_file", "find_files", "search_code"}


def test_do_mode_tools_schema_contains_all_core_tools():
    registry = create_default_registry()

    tools = registry.to_openai_tools(allowed_tool_names(AgentMode.DO))

    assert tool_schema_names(tools) == {
        "read_file",
        "write_file",
        "replace_in_file",
        "run_command",
        "find_files",
        "search_code",
    }


def test_agent_loop_passes_plan_mode_tools_to_provider(tmp_path):
    provider = FakeProvider([[TextDelta("计划")]])
    loop = AgentLoop(
        provider=provider,
        session=ChatSession(),
        registry=create_default_registry(),
        tool_context=ToolContext(workspace_root=tmp_path),
    )

    list(loop.run("先做计划", mode=AgentMode.PLAN))

    assert tool_schema_names(provider.calls[0]["tools"]) == {
        "read_file",
        "find_files",
        "search_code",
    }


def test_agent_loop_passes_do_mode_tools_to_provider(tmp_path):
    provider = FakeProvider([[TextDelta("执行")]])
    loop = AgentLoop(
        provider=provider,
        session=ChatSession(),
        registry=create_default_registry(),
        tool_context=ToolContext(workspace_root=tmp_path),
    )

    list(loop.run("执行任务", mode=AgentMode.DO))

    assert tool_schema_names(provider.calls[0]["tools"]) == {
        "read_file",
        "write_file",
        "replace_in_file",
        "run_command",
        "find_files",
        "search_code",
    }


def test_plan_mode_does_not_execute_write_file(tmp_path):
    write_tool = FakeTool("write_file")
    registry = registry_with(write_tool)
    provider = FakeProvider([[ToolCallEvent([tool_call("write_file")])]])
    loop = AgentLoop(
        provider=provider,
        session=ChatSession(),
        registry=registry,
        tool_context=ToolContext(workspace_root=tmp_path),
    )

    events = list(loop.run("计划时写文件", mode=AgentMode.PLAN))

    assert write_tool.calls == 0
    assert tool_errors(events)[0].code == "disallowed_tool"
    assert stopped_events(events)[-1].reason == StopReason.DISALLOWED_TOOL_CALL


def test_plan_mode_does_not_execute_replace_in_file(tmp_path):
    replace_tool = FakeTool("replace_in_file")
    registry = registry_with(replace_tool)
    provider = FakeProvider([[ToolCallEvent([tool_call("replace_in_file")])]])
    loop = AgentLoop(
        provider=provider,
        session=ChatSession(),
        registry=registry,
        tool_context=ToolContext(workspace_root=tmp_path),
    )

    events = list(loop.run("计划时改文件", mode=AgentMode.PLAN))

    assert replace_tool.calls == 0
    assert tool_errors(events)[0].code == "disallowed_tool"
    assert stopped_events(events)[-1].reason == StopReason.DISALLOWED_TOOL_CALL


def test_plan_mode_does_not_execute_run_command(tmp_path):
    command_tool = FakeTool("run_command")
    registry = registry_with(command_tool)
    provider = FakeProvider([[ToolCallEvent([tool_call("run_command")])]])
    loop = AgentLoop(
        provider=provider,
        session=ChatSession(),
        registry=registry,
        tool_context=ToolContext(workspace_root=tmp_path),
    )

    events = list(loop.run("计划时执行命令", mode=AgentMode.PLAN))

    assert command_tool.calls == 0
    assert tool_errors(events)[0].code == "disallowed_tool"
    assert stopped_events(events)[-1].reason == StopReason.DISALLOWED_TOOL_CALL


def test_unknown_tool_and_disallowed_tool_are_distinct(tmp_path):
    provider = FakeProvider([[ToolCallEvent([tool_call("missing_tool")])]])
    loop = AgentLoop(
        provider=provider,
        session=ChatSession(),
        registry=create_default_registry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        config=AgentLoopConfig(unknown_tool_threshold=1),
    )

    events = list(loop.run("未知工具", mode=AgentMode.PLAN))

    assert tool_errors(events)[0].code == "unknown_tool"
    assert stopped_events(events)[-1].reason == StopReason.UNKNOWN_TOOL_LIMIT
    assert stopped_events(events)[-1].reason != StopReason.DISALLOWED_TOOL_CALL
