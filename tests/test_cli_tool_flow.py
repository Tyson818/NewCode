from io import StringIO

from newcode import cli
from newcode.providers.base import TextDelta, ToolCallEvent
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
    assert [message.role for message in session.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert "文件内容" in session.messages[2].content


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
