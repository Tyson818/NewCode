"""Hook 纯模型和上下文边界。"""

import pytest

from newcode.hooks import (
    HookAction, HookActionType, HookContext, HookEvent, HookRule,
    HookScope, HookScopeIdentity, HookSource, HookValidationError,
)


def _rule(**changes):
    values = dict(id="safe_1", event=HookEvent.BEFORE_TOOL,
                  action=HookAction(HookActionType.SUBAGENT, {}), source=HookSource.USER)
    values.update(changes)
    return HookRule(**values)


@pytest.mark.parametrize("bad_id", ["", "A", "a.b", "a/../b", "a" * 65])
def test_rule_id_is_stable_safe_slug(bad_id):
    with pytest.raises(HookValidationError, match="hook_rule_invalid"):
        _rule(id=bad_id)


def test_before_tool_is_only_deny_event_and_must_be_sync():
    assert _rule(deny=True, reason="blocked").deny
    with pytest.raises(HookValidationError, match="hook_rule_invalid"):
        _rule(async_requested=True)
    with pytest.raises(HookValidationError, match="hook_rule_invalid"):
        _rule(event=HookEvent.TURN_START, deny=True)


def test_context_only_exposes_allowed_redacted_summary():
    context = HookContext(
        HookEvent.USER_MESSAGE_RECEIVED,
        {"message.summary": "prefix secret-value " + "x" * 600,
         "complete_prompt": "do not expose", "tool.result.full": "private"},
        sensitive_values=("secret-value",),
    )
    assert len(context.fields["message.summary"]) == 512
    assert "secret-value" not in context.fields["message.summary"]
    assert "do not expose" not in str(context.fields)
    assert "private" not in str(context.fields)
    assert "message.summary" not in HookContext(
        HookEvent.TURN_START, {"message.summary": "secret"}
    ).fields


def test_oversized_non_message_field_is_unavailable():
    context = HookContext(HookEvent.BEFORE_TOOL, {"tool.name": "a" * 1025})
    assert "tool.name" not in context.fields


def test_message_sensitive_key_and_invalid_args_do_not_enter_context():
    context = HookContext(HookEvent.USER_MESSAGE_RECEIVED, {
        "message.summary": "API_KEY=not-for-model safe text",
        "tool.normalized_args": {"bad": object()},
    })
    assert "not-for-model" not in context.fields["message.summary"]
    assert "tool.normalized_args" not in context.fields


@pytest.mark.parametrize(("event", "fields", "scope", "parts"), [
    (HookEvent.SYSTEM_START, {}, HookScope.SYSTEM, ("process-1",)),
    (HookEvent.SESSION_START, {"session.id": "s1"}, HookScope.SESSION, ("s1",)),
    (HookEvent.TURN_START, {"session.id": "s1", "turn.index": 2}, HookScope.TURN, ("s1", "2")),
    (HookEvent.USER_MESSAGE_RECEIVED, {"session.id": "s1", "message.id": "m1"}, HookScope.MESSAGE, ("s1", "m1")),
    (HookEvent.BEFORE_TOOL, {"session.id": "s1", "tool.call_id": "c1"}, HookScope.TOOL, ("s1", "c1")),
])
def test_five_once_scope_identities(event, fields, scope, parts):
    identity = HookScopeIdentity.from_context(HookContext(event, fields), process_id="process-1")
    assert (identity.scope, identity.parts) == (scope, parts)
