from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from newcode.agent import (
    AgentFinalAnswer,
    AgentIterationStarted,
    AgentLoop,
    AgentLoopConfig,
    AgentStopped,
    AgentTextDelta,
    AgentToolError,
    AgentToolResult,
    StopReason,
)
from newcode.agent.mode import AgentMode
from newcode.providers.base import ProviderError, ProviderEvent, TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.tools.registry import ToolRegistry
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


class ImmediateErrorProvider:
    def __init__(self) -> None:
        self.calls = 0

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls += 1
        raise ProviderError("provider failed")


class StreamingErrorProvider:
    def __init__(self) -> None:
        self.calls = 0

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls += 1
        yield TextDelta("partial")
        raise ProviderError("stream failed")


class SpyPromptBuilder:
    def __init__(self) -> None:
        self.calls = []

    def build_messages(self, session_messages, context):
        self.calls.append(
            {
                "session_messages": list(session_messages),
                "context": context,
            }
        )
        return [
            *session_messages,
        ]


@dataclass
class FakeTool:
    name: str = "read_file"
    ok: bool = True
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
        if self.ok:
            return ToolResult.success(self.name, {"arguments": arguments})
        return ToolResult.failure(self.name, "fake_error", "工具失败")


def make_loop(provider, tmp_path, *, registry=None, config=None, session=None):
    return AgentLoop(
        provider=provider,
        session=session or ChatSession(),
        registry=registry or make_registry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        config=config,
    )


def make_registry(*tools: FakeTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools or (FakeTool(),):
        registry.register(tool)
    return registry


def tool_call(name="read_file", call_id="call_1", arguments=None) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments or {"path": "README.md"},
        raw_arguments="{}",
    )


def stopped_events(events):
    return [event for event in events if isinstance(event, AgentStopped)]


def tool_messages(messages):
    return [message for message in messages if message.role == "tool"]


def system_messages(messages):
    return [message for message in messages if message.role == "system"]


def assert_prompt_messages(messages, *, iteration: int):
    systems = system_messages(messages)
    assert len(systems) >= 2
    assert "NewCode" in systems[0].content
    assert "不要自称" in systems[0].content
    assert "Claude" in systems[0].content
    assert "ChatGPT" in systems[0].content
    assert "Codex" in systems[0].content
    assert "<system-reminder>" in systems[1].content
    assert f"{iteration}/8" in systems[1].content


def assert_session_has_no_prompt_pollution(session):
    assert all(message.role != "system" for message in session.messages)
    assert all("<system-reminder>" not in (message.content or "") for message in session.messages)


def test_config_defaults():
    config = AgentLoopConfig()

    assert config.max_iterations == 8
    assert config.unknown_tool_threshold == 2
    assert config.tool_error_threshold == 3


def test_plain_chat_outputs_text_final_answer_and_session(tmp_path):
    session = ChatSession()
    provider = FakeProvider([[TextDelta("你"), TextDelta("好")]])
    loop = make_loop(provider, tmp_path, session=session)

    events = list(loop.run("打个招呼"))

    assert events[0] == AgentIterationStarted(iteration=1, max_iterations=8)
    assert AgentTextDelta("你") in events
    assert AgentTextDelta("好") in events
    assert events[-1] == AgentFinalAnswer("你好")
    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert session.messages[0].content == "打个招呼"
    assert session.messages[1].content == "你好"
    assert len(provider.calls) == 1
    assert_prompt_messages(provider.calls[0]["messages"], iteration=1)
    assert_session_has_no_prompt_pollution(session)


def test_agent_loop_uses_injected_prompt_builder_with_context(tmp_path):
    provider = FakeProvider([[TextDelta("ok")]])
    session = ChatSession()
    prompt_builder = SpyPromptBuilder()
    loop = AgentLoop(
        provider=provider,
        session=session,
        registry=make_registry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        config=AgentLoopConfig(max_iterations=3),
        prompt_builder=prompt_builder,
    )

    events = list(loop.run("规划", mode=AgentMode.PLAN))

    assert events[-1] == AgentFinalAnswer("ok")
    assert len(prompt_builder.calls) == 1
    context = prompt_builder.calls[0]["context"]
    assert context.mode is AgentMode.PLAN
    assert context.iteration == 1
    assert context.max_iterations == 3
    assert context.environment.workspace_root == str(tmp_path)
    assert context.environment.platform
    assert prompt_builder.calls[0]["session_messages"] == session.messages[:-1]
    assert provider.calls[0]["messages"] == prompt_builder.calls[0]["session_messages"]
    assert_session_has_no_prompt_pollution(session)


def test_single_tool_call_runs_tool_and_continues_to_final_answer(tmp_path):
    tool = FakeTool()
    registry = make_registry(tool)
    session = ChatSession()
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call(call_id="call_1")])],
            [TextDelta("已读取")],
        ]
    )
    loop = make_loop(provider, tmp_path, registry=registry, session=session)

    events = list(loop.run("读取文件"))

    assert len(provider.calls) == 2
    assert tool.calls == 1
    assert any(isinstance(event, AgentToolResult) for event in events)
    assert events[-1] == AgentFinalAnswer("已读取")
    assert [message.role for message in session.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert session.messages[1].tool_calls[0].id == "call_1"
    assert session.messages[2].tool_call_id == "call_1"
    assert "arguments" in session.messages[2].content
    assert_prompt_messages(provider.calls[0]["messages"], iteration=1)
    assert_prompt_messages(provider.calls[1]["messages"], iteration=2)
    assert [message.role for message in provider.calls[1]["messages"][-3:]] == [
        "user",
        "assistant",
        "tool",
    ]
    assert_session_has_no_prompt_pollution(session)


def test_multiple_tool_rounds_continue_until_final_answer(tmp_path):
    tool = FakeTool()
    registry = make_registry(tool)
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call(call_id="call_1")])],
            [ToolCallEvent([tool_call(call_id="call_2")])],
            [TextDelta("完成")],
        ]
    )
    loop = make_loop(provider, tmp_path, registry=registry)

    events = list(loop.run("连续处理"))

    assert len(provider.calls) == 3
    assert tool.calls == 2
    assert [
        event.iteration
        for event in events
        if isinstance(event, AgentIterationStarted)
    ] == [1, 2, 3]
    assert events[-1] == AgentFinalAnswer("完成")
    assert_prompt_messages(provider.calls[0]["messages"], iteration=1)
    assert_prompt_messages(provider.calls[1]["messages"], iteration=2)
    assert_prompt_messages(provider.calls[2]["messages"], iteration=3)
    assert_session_has_no_prompt_pollution(loop.session)


def test_max_iterations_stops(tmp_path):
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call(call_id="call_1")])],
            [ToolCallEvent([tool_call(call_id="call_2")])],
        ]
    )
    loop = make_loop(
        provider,
        tmp_path,
        config=AgentLoopConfig(max_iterations=2),
    )

    events = list(loop.run("一直调工具"))

    assert len(provider.calls) == 2
    assert stopped_events(events)[-1].reason == StopReason.MAX_ITERATIONS


def test_immediate_provider_error_stops(tmp_path):
    provider = ImmediateErrorProvider()
    loop = make_loop(provider, tmp_path)

    events = list(loop.run("触发错误"))

    assert provider.calls == 1
    assert stopped_events(events)[-1].reason == StopReason.PROVIDER_ERROR
    assert "provider failed" in stopped_events(events)[-1].message


def test_streaming_provider_error_stops_after_partial_text(tmp_path):
    provider = StreamingErrorProvider()
    loop = make_loop(provider, tmp_path)

    events = list(loop.run("触发流错误"))

    assert AgentTextDelta("partial") in events
    assert stopped_events(events)[-1].reason == StopReason.PROVIDER_ERROR
    assert "stream failed" in stopped_events(events)[-1].message


def test_consecutive_unknown_tool_limit_stops(tmp_path):
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call(name="missing", call_id="call_1")])],
            [ToolCallEvent([tool_call(name="missing", call_id="call_2")])],
        ]
    )
    loop = make_loop(provider, tmp_path)

    events = list(loop.run("请求未知工具"))

    assert len(provider.calls) == 2
    assert [
        event.code
        for event in events
        if isinstance(event, AgentToolError)
    ] == ["unknown_tool", "unknown_tool"]
    assert stopped_events(events)[-1].reason == StopReason.UNKNOWN_TOOL_LIMIT


def test_tool_error_limit_stops(tmp_path):
    failing_tool = FakeTool(ok=False)
    registry = make_registry(failing_tool)
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call(call_id="call_1")])],
            [ToolCallEvent([tool_call(call_id="call_2")])],
            [ToolCallEvent([tool_call(call_id="call_3")])],
        ]
    )
    loop = make_loop(provider, tmp_path, registry=registry)

    events = list(loop.run("工具持续失败"))

    assert len(provider.calls) == 3
    assert failing_tool.calls == 3
    assert stopped_events(events)[-1].reason == StopReason.TOOL_ERROR_LIMIT


def test_cancel_flag_set_before_run_does_not_call_provider(tmp_path):
    provider = FakeProvider([[TextDelta("不应调用")]])
    loop = make_loop(provider, tmp_path)

    events = list(loop.run("取消", cancel_flag=True))

    assert provider.calls == []
    assert len(events) == 1
    assert events[0].reason == StopReason.USER_CANCELLED
    assert events[0].iteration == 0


def test_agent_loop_writes_parallel_tool_results_in_original_order(tmp_path):
    registry = make_registry(
        FakeTool(name="read_file"),
        FakeTool(name="find_files"),
        FakeTool(name="search_code"),
    )
    session = ChatSession()
    provider = FakeProvider(
        [
            [
                ToolCallEvent(
                    [
                        tool_call(name="read_file", call_id="call_1"),
                        tool_call(name="find_files", call_id="call_2"),
                        tool_call(name="search_code", call_id="call_3"),
                    ]
                )
            ],
            [TextDelta("done")],
        ]
    )
    loop = make_loop(provider, tmp_path, registry=registry, session=session)

    events = list(loop.run("run tools"))

    assert events[-1] == AgentFinalAnswer("done")
    assert [message.tool_call_id for message in session.messages if message.role == "tool"] == [
        "call_1",
        "call_2",
        "call_3",
    ]
    assert [message.tool_call_id for message in tool_messages(provider.calls[1]["messages"])] == [
        "call_1",
        "call_2",
        "call_3",
    ]


def test_agent_loop_default_mode_exposes_all_tools(tmp_path):
    provider = FakeProvider([[TextDelta("ok")]])
    loop = AgentLoop(
        provider=provider,
        session=ChatSession(),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
    )
    for tool_name in [
        "read_file",
        "write_file",
        "replace_in_file",
        "run_command",
        "find_files",
        "search_code",
    ]:
        loop.registry.register(FakeTool(name=tool_name))

    list(loop.run("默认模式"))

    assert {tool["function"]["name"] for tool in provider.calls[0]["tools"]} == {
        "read_file",
        "write_file",
        "replace_in_file",
        "run_command",
        "find_files",
        "search_code",
        "load_skill",
    }


def test_agent_loop_explicit_do_mode_exposes_all_tools(tmp_path):
    provider = FakeProvider([[TextDelta("ok")]])
    loop = make_loop(provider, tmp_path)

    list(loop.run("do mode", mode=AgentMode.DO))

    assert {tool["function"]["name"] for tool in provider.calls[0]["tools"]} == {
        "read_file",
        "load_skill",
    }
