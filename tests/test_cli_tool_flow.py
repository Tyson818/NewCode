from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
import sys

from newcode import cli
from newcode.config import AppConfig
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.providers.deepseek import (
    DSML_TOOL_CALLS_END,
    DSML_TOOL_CALLS_START,
    DeepSeekProvider,
)
from newcode.session import ChatSession
from newcode.tools.registry import create_default_registry
from newcode.tools.types import ToolCall, ToolContext


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
        return self.values.pop(0)


def run_flow(provider, tmp_path, prompt="读文件"):
    output = StringIO()
    error = StringIO()
    session = ChatSession()
    code = cli.run_conversation(
        provider,
        session,
        registry=create_default_registry(),
        tool_context=ToolContext(workspace_root=tmp_path, sensitive_values=("secret-value",)),
        input_func=PromptRecorder([prompt, "/exit"]),
        output=output,
        error_output=error,
    )
    return code, output.getvalue(), error.getvalue(), session


def test_cli_executes_one_tool_call_and_feeds_result_back(tmp_path):
    (tmp_path / "a.txt").write_text("文件内容", encoding="utf-8")
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
            [TextDelta("读到了文件内容")],
        ]
    )

    code, output, error, session = run_flow(provider, tmp_path)

    assert code == 0
    assert error == ""
    assert "读到了文件内容" in output
    assert provider.calls[0]["tools"]
    assert provider.calls[0]["allow_tool_calls"] is True
    assert provider.calls[1]["tools"]
    assert provider.calls[1]["allow_tool_calls"] is True
    assert [message.role for message in session.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert "文件内容" in session.messages[2].content


def test_cli_executes_multiple_tool_calls_without_old_single_tool_limit(tmp_path):
    (tmp_path / "a.txt").write_text("A", encoding="utf-8")
    (tmp_path / "b.txt").write_text("B", encoding="utf-8")
    provider = FakeProvider(
        [
            [
                ToolCallEvent(
                    [
                        ToolCall(id="call_1", name="read_file", arguments={"path": "a.txt"}),
                        ToolCall(id="call_2", name="read_file", arguments={"path": "b.txt"}),
                    ]
                )
            ],
            [TextDelta("两个文件都读完了")],
        ]
    )

    _, output, _, session = run_flow(provider, tmp_path)

    assert "两个文件都读完了" in output
    assert "multiple_tool_calls_not_supported" not in session.messages[2].content
    assert "A" in session.messages[2].content
    assert "B" in session.messages[3].content


def test_cli_stops_instead_of_looping_forever_when_model_keeps_requesting_tools(tmp_path):
    (tmp_path / "a.txt").write_text("ok", encoding="utf-8")
    provider = FakeProvider(
        [
            [
                ToolCallEvent(
                    [
                        ToolCall(
                            id=f"call_{index}",
                            name="read_file",
                            arguments={"path": "a.txt"},
                        )
                    ]
                )
            ]
            for index in range(1, 10)
        ]
    )

    _, output, _, _ = run_flow(provider, tmp_path)

    assert len(provider.calls) == 8
    assert "已达到最大迭代次数" in output


def test_cli_masks_sensitive_tool_result(tmp_path):
    provider = FakeProvider(
        [
            [
                ToolCallEvent(
                    [
                        ToolCall(
                            id="call_1",
                            name="run_command",
                            arguments={"command": "echo secret-value"},
                            raw_arguments='{"command": "echo secret-value"}',
                        )
                    ]
                )
            ],
            [TextDelta("已处理")],
        ]
    )

    _, _, _, session = run_flow(provider, tmp_path)

    assert "secret-value" not in session.messages[2].content
    assert "[REDACTED]" in session.messages[2].content


class FakeCompletions:
    def __init__(self, streams):
        self.streams = streams
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.streams[min(len(self.calls) - 1, len(self.streams) - 1)]


class FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def content_chunk(content):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=None)
            )
        ]
    )


def provider_tool_messages(messages):
    return [message for message in messages if message["role"] == "tool"]


def dsml_tag(name: str, closing: bool = False) -> str:
    tag = DSML_TOOL_CALLS_START
    tag = tag.replace("tool_calls", name)
    if closing:
        tag = tag.replace("<", "</", 1)
    return tag


def dsml_invoke_start(name: str) -> str:
    return dsml_tag(f'invoke name="{name}"')


def dsml_parameter(name: str, value: str, *, is_string: bool = True) -> str:
    string_value = "true" if is_string else "false"
    tag_name = f'parameter name="{name}" string="{string_value}"'
    return (
        f"{dsml_tag(tag_name)}"
        f"{value}"
        f"{dsml_tag('parameter', closing=True)}"
    )


def run_deepseek_flow(provider, tmp_path):
    output = StringIO()
    error = StringIO()
    session = ChatSession()
    code = cli.run_conversation(
        provider,
        session,
        registry=create_default_registry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        input_func=PromptRecorder(["请使用工具列出当前项目根目录下的文件。", "/exit"]),
        output=output,
        error_output=error,
    )
    return code, output.getvalue(), error.getvalue(), session


def test_cli_parses_deepseek_dsml_runs_command_and_hides_dsml(tmp_path):
    command = f'"{sys.executable}" -c "print(\'tool-ok\')"'
    dsml = (
        f"{DSML_TOOL_CALLS_START}"
        f"{dsml_invoke_start('run_command')}"
        f"{dsml_parameter('command', command)}"
        f"{dsml_parameter('timeout_seconds', '10', is_string=False)}"
        f"{dsml_tag('invoke', closing=True)}"
        f"{DSML_TOOL_CALLS_END}"
    )
    completions = FakeCompletions(
        [
            [content_chunk(dsml[:60]), content_chunk(dsml[60:])],
            [content_chunk("命令已经执行，输出是 tool-ok。")],
        ]
    )
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    code, output, error, session = run_deepseek_flow(provider, tmp_path)

    assert code == 0
    assert error == ""
    assert "DSML" not in output
    assert "命令已经执行，输出是 tool-ok。" in output
    assert len(completions.calls) == 2
    assert completions.calls[0]["tool_choice"] == "auto"
    assert completions.calls[1]["tool_choice"] == "auto"
    tool_message = provider_tool_messages(completions.calls[1]["messages"])[0]
    assert tool_message["role"] == "tool"
    assert "tool-ok" in tool_message["content"]
    assert sum(message.role == "tool" for message in session.messages) == 1


def test_cli_hides_dsml_when_start_marker_is_split_across_chunks(tmp_path):
    command = f'"{sys.executable}" -c "print(\'split-ok\')"'
    dsml_body = (
        f"{dsml_invoke_start('run_command')}"
        f"{dsml_parameter('command', command)}"
        f"{dsml_tag('invoke', closing=True)}"
        f"{DSML_TOOL_CALLS_END}"
    )
    completions = FakeCompletions(
        [
            [
                content_chunk(DSML_TOOL_CALLS_START[:5]),
                content_chunk(DSML_TOOL_CALLS_START[5:13]),
                content_chunk(DSML_TOOL_CALLS_START[13:]),
                content_chunk(dsml_body),
            ],
            [content_chunk("命令已经执行，输出是 split-ok。")],
        ]
    )
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )

    code, output, error, session = run_deepseek_flow(provider, tmp_path)

    assert code == 0
    assert error == ""
    assert "DSML" not in output
    assert "命令已经执行，输出是 split-ok。" in output
    assert len(completions.calls) == 2
    tool_message = provider_tool_messages(completions.calls[1]["messages"])[0]
    assert tool_message["role"] == "tool"
    assert "split-ok" in tool_message["content"]
    assert sum(message.role == "tool" for message in session.messages) == 1
