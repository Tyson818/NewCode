from newcode.context.manager import ContextManager
from newcode.session import ChatSession


def test_three_failed_attempts_open_circuit(tmp_path):
    session = ChatSession()
    for index in range(6):
        session.add_user_message(str(index))
    manager = ContextManager(session, tmp_path)
    for _ in range(3):
        assert not manager.retry_manual(lambda prompt: "bad")
    assert manager.circuit_open


def test_only_trusted_usage_updates_anchor(tmp_path):
    session = ChatSession()
    manager = ContextManager(session, tmp_path)
    assert manager.record_usage({"prompt_tokens": 42})
    assert manager.estimator.anchor is not None
    assert not manager.record_usage({"prompt_tokens": -1})
    assert manager.estimator.anchor.prompt_tokens == 42


def test_usage_anchor_can_trigger_automatic_compaction(tmp_path):
    session = ChatSession()
    for index in range(6):
        session.add_user_message("x" * 5_000 + str(index))
    manager = ContextManager(session, tmp_path)
    manager.record_usage({"prompt_tokens": 51_000})
    assert manager.prepare(lambda prompt: "bad") is False
    assert manager.consecutive_failures == 1
