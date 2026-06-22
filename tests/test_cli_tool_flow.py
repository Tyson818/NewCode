from io import StringIO
from types import SimpleNamespace
import sys

from newcode import cli
from newcode.config import AppConfig
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.providers.deepseek import (
    DISALLOWED_TOOL_CALL_TEXT,
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
        yield from self.event_batches[len(self.calls) - 1]


class PromptRecorder:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self, prompt):
        if not self.values:
            raise EOFError
        return self.values.pop(0)


def run_flow(provider, tmp_path):
    output = StringIO()
    error = StringIO()
    session = ChatSession()
    code = cli.run_conversation(
        provider,
        session,
        registry=create_default_registry(),
        tool_context=ToolContext(workspace_root=tmp_path, sensitive_values=("secret-value",)),
        input_func=PromptRecorder(["读文件", "/exit"]),
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
    assert provider.calls[1]["tools"] is None
    assert provider.calls[1]["allow_tool_calls"] is False
    assert_final_answer_instruction(provider.calls[1]["messages"][-1])
    assert [message.role for message in session.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert "文件内容" in session.messages[2].content


    assert all(cli.FINAL_ANSWER_INSTRUCTION != message.content for message in session.messages)


def test_cli_returns_failure_for_multiple_tool_calls_without_executing_both(tmp_path):
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
            [TextDelta("本阶段不能一次执行多个工具")],
        ]
    )

    _, output, _, session = run_flow(provider, tmp_path)

    assert "本阶段不能一次执行多个工具" in output
    assert "multiple_tool_calls_not_supported" in session.messages[2].content
    assert "multiple_tool_calls_not_supported" in session.messages[3].content
    assert "A" not in session.messages[2].content
    assert "B" not in session.messages[3].content


def test_cli_does_not_loop_when_final_response_requests_tool(tmp_path):
    provider = FakeProvider(
        [
            [
                ToolCallEvent(
                    [
                        ToolCall(
                            id="call_1",
                            name="read_file",
                            arguments={"path": "missing.txt"},
                            raw_arguments='{"path": "missing.txt"}',
                        )
                    ]
                )
            ],
            [
                ToolCallEvent(
                    [
                        ToolCall(
                            id="call_2",
                            name="read_file",
                            arguments={"path": "again.txt"},
                        )
                    ]
                )
            ],
        ]
    )

    _, output, _, _ = run_flow(provider, tmp_path)

    assert len(provider.calls) == 2
    assert "本阶段不会继续执行" in output


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
        return self.streams[len(self.calls) - 1]


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


def assert_final_answer_instruction(message):
    content = message["content"] if isinstance(message, dict) else message.content
    role = message["role"] if isinstance(message, dict) else message.role
    assert role == "user"
    assert "最终回答阶段" in content
    assert "禁止再次调用工具" in content
    assert "禁止输出 DSML/tool_calls" in content
    assert "自然语言回答" in content


def test_cli_parses_deepseek_dsml_runs_command_and_hides_dsml(tmp_path):
    command = f'"{sys.executable}" -c "print(\'tool-ok\')"'
    dsml = (
        "<｜｜DSML｜｜tool_calls>"
        '<｜｜DSML｜｜invoke name="run_command">'
        f'<｜｜DSML｜｜parameter name="command" string="true">{command}</｜｜DSML｜｜parameter>'
        '<｜｜DSML｜｜parameter name="timeout_seconds" string="false">10</｜｜DSML｜｜parameter>'
        "</｜｜DSML｜｜invoke>"
        "</｜｜DSML｜｜tool_calls>"
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

    assert code == 0
    assert error.getvalue() == ""
    assert "<｜｜DSML｜｜tool_calls>" not in output.getvalue()
    assert "命令已经执行，输出是 tool-ok。" in output.getvalue()
    assert len(completions.calls) == 2
    assert completions.calls[0]["tool_choice"] == "auto"
    assert completions.calls[1].get("tools") is None
    assert completions.calls[1].get("tool_choice") is None
    assert_final_answer_instruction(completions.calls[1]["messages"][-1])
    tool_message = completions.calls[1]["messages"][2]
    assert tool_message["role"] == "tool"
    assert "tool-ok" in tool_message["content"]
    assert all(cli.FINAL_ANSWER_INSTRUCTION != message.content for message in session.messages)


def test_cli_hides_dsml_when_start_marker_is_split_across_chunks(tmp_path):
    command = f'"{sys.executable}" -c "print(\'split-ok\')"'
    dsml_body = (
        '<｜｜DSML｜｜invoke name="run_command">'
        f'<｜｜DSML｜｜parameter name="command" string="true">{command}</｜｜DSML｜｜parameter>'
        "</｜｜DSML｜｜invoke>"
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

    assert code == 0
    assert error.getvalue() == ""
    assert "DSML" not in output.getvalue()
    assert "命令已经执行，输出是 split-ok。" in output.getvalue()
    assert len(completions.calls) == 2
    assert_final_answer_instruction(completions.calls[1]["messages"][-1])
    tool_message = completions.calls[1]["messages"][2]
    assert tool_message["role"] == "tool"
    assert "split-ok" in tool_message["content"]
    assert all(cli.FINAL_ANSWER_INSTRUCTION != message.content for message in session.messages)


def test_cli_does_not_execute_dsml_returned_during_final_response(tmp_path):
    first_command = f'"{sys.executable}" -c "print(\'first-ok\')"'
    second_command = f'"{sys.executable}" -c "print(\'second-should-not-run\')"'
    first_dsml = (
        f"{DSML_TOOL_CALLS_START}"
        '<｜｜DSML｜｜invoke name="run_command">'
        f'<｜｜DSML｜｜parameter name="command" string="true">{first_command}</｜｜DSML｜｜parameter>'
        "</｜｜DSML｜｜invoke>"
        f"{DSML_TOOL_CALLS_END}"
    )
    second_dsml = (
        f"{DSML_TOOL_CALLS_START}"
        '<｜｜DSML｜｜invoke name="run_command">'
        f'<｜｜DSML｜｜parameter name="command" string="true">{second_command}</｜｜DSML｜｜parameter>'
        "</｜｜DSML｜｜invoke>"
        f"{DSML_TOOL_CALLS_END}"
    )
    completions = FakeCompletions(
        [
            [content_chunk(first_dsml)],
            [content_chunk(second_dsml)],
        ]
    )
    provider = DeepSeekProvider(
        AppConfig(model="deepseek-chat"),
        api_key="secret",
        client=FakeClient(completions),
    )
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

    assert code == 0
    assert error.getvalue() == ""
    assert len(completions.calls) == 2
    assert completions.calls[1].get("tools") is None
    assert completions.calls[1].get("tool_choice") is None
    assert_final_answer_instruction(completions.calls[1]["messages"][-1])
    assert DISALLOWED_TOOL_CALL_TEXT in output.getvalue()
    assert "DSML" not in output.getvalue()
    assert sum(message.role == "tool" for message in session.messages) == 1
    assert "first-ok" in session.messages[2].content
    assert "second-should-not-run" not in session.messages[2].content
    assert all(cli.FINAL_ANSWER_INSTRUCTION != message.content for message in session.messages)
