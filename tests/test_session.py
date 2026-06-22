import json

import pytest

from newcode.session import ChatMessage, ChatSession
from newcode.tools.types import ToolCall, ToolResult


def test_session_starts_empty():
    session = ChatSession()

    assert session.messages == []
    assert session.to_provider_messages() == []


def test_session_keeps_messages_in_order():
    session = ChatSession()

    session.add_user_message("你好")
    session.add_assistant_message("你好，我是 NewCode")
    session.add_user_message("继续")

    assert session.to_provider_messages() == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，我是 NewCode"},
        {"role": "user", "content": "继续"},
    ]


def test_session_ignores_blank_messages():
    session = ChatSession()

    session.add_user_message("   ")
    session.add_assistant_message("\n")

    assert session.messages == []


def test_session_serializes_tool_calls_and_results():
    session = ChatSession()
    tool_call = ToolCall(
        id="call_1",
        name="read_file",
        arguments={"path": "pyproject.toml"},
        raw_arguments='{"path": "pyproject.toml"}',
    )

    session.add_user_message("读文件")
    session.add_assistant_tool_call(tool_call)
    session.add_tool_result("call_1", ToolResult.success("read_file", {"content": "ok"}))

    messages = session.to_provider_messages()

    assert messages[1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "pyproject.toml"}',
                },
            }
        ],
    }
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "call_1"
    assert json.loads(messages[2]["content"])["ok"] is True


def test_message_rejects_invalid_role():
    with pytest.raises(ValueError):
        ChatMessage(role="developer", content="nope")  # type: ignore[arg-type]
