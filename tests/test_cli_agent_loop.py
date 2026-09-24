from __future__ import annotations

from io import StringIO

import pytest

from newcode import cli
from newcode.agent.mode import AgentMode
from newcode.hooks.types import HookLoadResult, HookNetworkPolicy
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.tools.registry import create_default_registry
from newcode.tools.types import ToolCall, ToolContext


@pytest.fixture(autouse=True)
def _isolated_hook_config(monkeypatch):
    """原有 AgentLoop CLI 测试不读取本机用户级 Hook 配置。"""

    monkeypatch.setattr(cli, "load_hook_rules", lambda _root: HookLoadResult((), HookNetworkPolicy()))


class FakeProvider:
    def __init__(self, event_batches):
        self.event_batches = event_batches
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "allow_tool_calls": allow_tool_calls,
            }
        )
        yield from self.event_batches[min(len(self.calls) - 1, len(self.event_batches) - 1)]


class PromptRecorder:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self, prompt):
        if not self.values:
            raise EOFError
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def run_cli(provider, tmp_path, inputs, session=None):
    output = StringIO()
    error = StringIO()
    session = session or ChatSession()
    code = cli.run_conversation(
        provider,
        session,
        registry=create_default_registry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        input_func=PromptRecorder(inputs),
        output=output,
        error_output=error,
    )
    return code, output.getvalue(), error.getvalue(), session


def tool_schema_names(tools):
    return {tool["function"]["name"] for tool in tools}


def tool_messages(messages):
    return [message for message in messages if message.role == "tool"]


def test_cli_plain_chat_uses_agent_loop(tmp_path):
    provider = FakeProvider([[TextDelta("你好")]])

    code, output, error, session = run_cli(provider, tmp_path, ["你好", "/exit"])

    assert code == 0
    assert error == ""
    assert "NewCode> 你好" in output
    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert len(provider.calls) == 1


def test_cli_single_tool_call_feeds_result_to_next_provider_request(tmp_path):
    (tmp_path / "a.txt").write_text("file-content", encoding="utf-8")
    provider = FakeProvider(
        [
            [
                ToolCallEvent(
                    [
                        ToolCall(
                            id="call_1",
                            name="read_file",
                            arguments={"path": "a.txt"},
                            raw_arguments='{"path": "a.txt"}',
                        )
                    ]
                )
            ],
            [TextDelta("读取完成")],
        ]
    )

    code, output, error, session = run_cli(provider, tmp_path, ["读文件", "/exit"])

    assert code == 0
    assert error == ""
    assert "[工具] read_file" in output
    assert "读取完成" in output
    assert len(provider.calls) == 2
    tool_message = tool_messages(provider.calls[1]["messages"])[0]
    assert tool_message.role == "tool"
    assert "file-content" in tool_message.content
    assert [message.role for message in session.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


def test_cli_plan_and_do_switch_modes_without_session_messages(tmp_path):
    provider = FakeProvider([[TextDelta("计划")], [TextDelta("执行")]])
    session = ChatSession()

    code, output, error, session = run_cli(
        provider,
        tmp_path,
        ["/plan", "先计划", "/do", "再执行", "/exit"],
        session,
    )

    assert code == 0
    assert error == ""
    assert "Plan Mode" in output
    assert "Do Mode" in output
    assert tool_schema_names(provider.calls[0]["tools"]) == {
        "read_file",
        "find_files",
        "search_code",
        "load_skill",
    }
    assert tool_schema_names(provider.calls[1]["tools"]) == {
        "read_file",
        "write_file",
        "replace_in_file",
        "run_command",
        "find_files",
        "search_code",
        "load_skill",
    }
    assert [message.content for message in session.messages if message.role == "user"] == [
        "先计划",
        "再执行",
    ]


def test_cli_keyboard_interrupt_before_input_does_not_call_provider(tmp_path):
    provider = FakeProvider([[TextDelta("不应调用")]])

    code, output, error, _ = run_cli(provider, tmp_path, [KeyboardInterrupt()])

    assert code == 0
    assert error == ""
    assert provider.calls == []
    assert "已中断对话" in output


def test_cli_keyboard_interrupt_during_agent_run_stops_current_task(tmp_path):
    class InterruptingProvider(FakeProvider):
        def stream_chat(self, messages, tools=None, allow_tool_calls=True):
            self.calls.append(
                {
                    "messages": list(messages),
                    "tools": tools,
                    "allow_tool_calls": allow_tool_calls,
                }
            )
            raise KeyboardInterrupt

    provider = InterruptingProvider([])

    code, output, error, _ = run_cli(provider, tmp_path, ["开始", "/exit"])

    assert code == 0
    assert error == ""
    assert len(provider.calls) == 1
    assert "已中断当前任务" in output
