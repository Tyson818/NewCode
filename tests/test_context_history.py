from newcode.context.history import compact_history, select_recent_start
from newcode.session import ChatSession

from tests.test_context_summary import _valid


def test_compaction_preserves_recent_user_message_and_adds_boundary():
    session = ChatSession()
    for index in range(6):
        session.add_user_message("x" * 5_000 + f"user-{index}")
    assert compact_history(session, lambda prompt: _valid())
    assert session.messages[-1].content == "x" * 5_000 + "user-5"
    assert any("需要细节" in (item.content or "") for item in session.messages)


def test_invalid_summary_leaves_history_unchanged():
    session = ChatSession()
    for index in range(6):
        session.add_user_message(f"user-{index}")
    before = list(session.messages)
    assert not compact_history(session, lambda prompt: "bad")
    assert session.messages == before
