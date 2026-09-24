"""HookEngine 的 once、顺序、拒绝与异常隔离。"""

import pytest

from newcode.hooks import (
    HookAction, HookActionType, HookContext, HookEngine, HookEvent,
    HookOutcomeStatus, HookRule, HookSource,
    HookValidationError,
)


def _rule(name, event=HookEvent.TURN_START, *, once=False, deny=False):
    return HookRule(name, event, HookAction(HookActionType.SUBAGENT, {}),
                    HookSource.USER, once=once, deny=deny)


def _context(event, **fields):
    return HookContext(event, fields)


def test_stable_order_skip_and_first_before_tool_deny():
    called = []
    rules = (_rule("first", HookEvent.BEFORE_TOOL),
             _rule("deny", HookEvent.BEFORE_TOOL, deny=True),
             _rule("after", HookEvent.BEFORE_TOOL))
    engine = HookEngine(rules, action_sink=lambda rule, _context, _generation: called.append(rule.id) or True)
    result = engine.emit(HookEvent.BEFORE_TOOL, _context(HookEvent.BEFORE_TOOL))
    assert called == ["first"]
    assert [item.status for item in result.outcomes] == [HookOutcomeStatus.SCHEDULED, HookOutcomeStatus.DENIED]
    assert result.denied and result.outcomes[-1].diagnostic_code == "hook_tool_denied"


def test_sink_exception_not_deny_and_next_rule_runs():
    called = []

    def sink(rule, context, generation):
        called.append(rule.id)
        if rule.id == "broken":
            raise RuntimeError("secret must not leak")
        return True

    engine = HookEngine((_rule("broken"), _rule("good")), action_sink=sink)
    result = engine.emit(HookEvent.TURN_START, _context(HookEvent.TURN_START))
    assert called == ["broken", "good"]
    assert not result.denied
    assert [item.status for item in result.outcomes] == [HookOutcomeStatus.FAILED, HookOutcomeStatus.SCHEDULED]
    assert "secret" not in str(result.diagnostics)


def test_untrusted_exception_code_is_normalized():
    def sink(_rule, _context, _generation):
        raise HookValidationError("secret-value")

    engine = HookEngine((_rule("one"),), action_sink=sink)
    result = engine.emit(HookEvent.TURN_START, _context(HookEvent.TURN_START))
    assert result.diagnostics[0].code == "hook_action_failed"
    assert "secret-value" not in str(result)


def test_hook_origin_and_reentrant_emit_are_suppressed():
    calls = []
    engine = None

    def sink(rule, context, generation):
        calls.append(rule.id)
        assert not engine.emit(context.event, context).outcomes
        return True

    engine = HookEngine((_rule("one"),), action_sink=sink)
    context = _context(HookEvent.TURN_START)
    assert not engine.emit(HookEvent.TURN_START, context, hook_origin=True).outcomes
    engine.emit(HookEvent.TURN_START, context)
    assert calls == ["one"]


@pytest.mark.parametrize(("event", "fields"), [
    (HookEvent.SYSTEM_START, {}),
    (HookEvent.SESSION_START, {"session.id": "s1"}),
    (HookEvent.TURN_START, {"session.id": "s1", "turn.index": 1}),
    (HookEvent.USER_MESSAGE_RECEIVED, {"session.id": "s1", "message.id": "m1"}),
    (HookEvent.BEFORE_TOOL, {"session.id": "s1", "tool.call_id": "c1"}),
])
def test_once_scope_and_reset(event, fields):
    engine = HookEngine((_rule("once", event, once=True),), process_id="p1")
    context = HookContext(event, fields)
    assert len(engine.emit(event, context).outcomes) == 1
    assert not engine.emit(event, context).outcomes
    engine.reset_session()
    assert len(engine.emit(event, context).outcomes) == (0 if event is HookEvent.SYSTEM_START else 1)
    assert len(HookEngine((_rule("once", event, once=True),), process_id="p2").emit(event, context).outcomes) == 1


def test_once_scope_instance_changes_for_message_tool_and_turn():
    for event, field in ((HookEvent.TURN_START, "turn.index"),
                         (HookEvent.USER_MESSAGE_RECEIVED, "message.id"),
                         (HookEvent.BEFORE_TOOL, "tool.call_id")):
        engine = HookEngine((_rule("once", event, once=True),))
        first = HookContext(event, {"session.id": "same", field: 1 if field == "turn.index" else "one"})
        second = HookContext(event, {"session.id": "same", field: 2 if field == "turn.index" else "two"})
        assert len(engine.emit(event, first).outcomes) == 1
        assert len(engine.emit(event, second).outcomes) == 1


def test_missing_once_scope_identity_is_safe_failure():
    engine = HookEngine((_rule("once", once=True),))
    result = engine.emit(HookEvent.TURN_START, _context(HookEvent.TURN_START))
    assert not result.denied
    assert result.outcomes[0].status is HookOutcomeStatus.FAILED


def test_reset_callback_failure_is_safely_diagnosed():
    def broken_reset(_generation):
        raise RuntimeError("secret-value")

    engine = HookEngine((), on_session_reset=broken_reset)
    engine.reset_session()
    assert engine.diagnostics[0].code == "hook_action_failed"
    assert "secret-value" not in str(engine.diagnostics)
