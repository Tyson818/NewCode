import pytest

from newcode.session import ChatMessage, ChatSession


def test_session_starts_empty():
    session = ChatSession()

    assert session.messages == []
    assert session.to_provider_messages() == []


def test_session_keeps_messages_in_order():
    session = ChatSession()

    session.add_user_message("你好")
    session.add_assistant_message("你好，我是 Newcode")
    session.add_user_message("继续")

    assert session.to_provider_messages() == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，我是 Newcode"},
        {"role": "user", "content": "继续"},
    ]


def test_session_ignores_blank_messages():
    session = ChatSession()

    session.add_user_message("   ")
    session.add_assistant_message("\n")

    assert session.messages == []


def test_message_rejects_invalid_role():
    with pytest.raises(ValueError):
        ChatMessage(role="tool", content="nope")  # type: ignore[arg-type]
