from newcode.context.estimator import TokenEstimator, estimate_message, estimate_messages
from newcode.session import ChatMessage, ChatSession
from newcode.tools.types import ToolCall


def test_estimate_is_stable_and_counts_tool_structure():
    plain = ChatMessage(role="user", content="你好")
    tool = ChatMessage(role="assistant", content=None, tool_calls=[ToolCall("1", "read_file")])
    assert estimate_messages([plain, tool]) == estimate_messages([plain, tool])
    assert estimate_message(tool) > estimate_message(plain)


def test_usage_anchor_invalidates_when_session_is_replaced():
    session = ChatSession()
    session.add_user_message("hello")
    estimator = TokenEstimator()
    assert estimator.record_usage(100, session.context_version)
    assert estimator.anchor is not None
    session.replace_messages(list(session.messages))
    assert estimator.estimate(session.messages, session.context_version) == estimate_messages(session.messages)
    assert not estimator.record_usage(-1, session.context_version)
