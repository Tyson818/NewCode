"""受限 Hook 条件匹配及正则复杂度保护。"""

from time import monotonic

import pytest

from newcode.hooks import HookContext, HookEvent, HookValidationError, condition_matches, parse_condition
from newcode.hooks import conditions as condition_module


def _context(**fields):
    return HookContext(HookEvent.USER_MESSAGE_RECEIVED, fields)


def test_exact_glob_regex_not_and_single_level_combinations():
    context = _context(**{"tool.name": "read_file", "mode": "do"})
    assert condition_matches(parse_condition({"field": "tool.name", "exact": "read_file"}), context)
    assert condition_matches(parse_condition({"field": "tool.name", "glob": "read_*"}), context)
    assert condition_matches(parse_condition({"field": "tool.name", "regex": "^read_[a-z]{4}$"}), context)
    assert condition_matches(parse_condition({"field": "tool.name", "exact": "write_file", "not": True}), context)
    assert condition_matches(parse_condition({"all": [
        {"field": "tool.name", "glob": "read_*"}, {"field": "mode", "exact": "do"},
    ]}), context)
    assert condition_matches(parse_condition({"any": [
        {"field": "mode", "exact": "plan"}, {"field": "tool.name", "exact": "read_file"},
    ]}), context)


def test_missing_field_never_matches_even_if_negated():
    condition = parse_condition({"field": "tool.call_id", "exact": "x", "not": True})
    assert not condition_matches(condition, _context())


@pytest.mark.parametrize("raw", [
    {"all": [], "any": []},
    {"all": [{"any": [{"field": "mode", "exact": "do"}]}]},
    {"field": "full_user_message", "exact": "secret"},
    {"field": "mode", "exact": "do", "glob": "*"},
    {"field": "mode", "regex": "(a+)+$"},
    {"field": "mode", "regex": "a|b"},
    {"field": "mode", "regex": "a" * 257},
    {"field": "mode", "regex": "a{1,257}"},
])
def test_unsafe_or_invalid_condition_is_rejected(raw):
    with pytest.raises(HookValidationError, match="hook_condition_invalid"):
        parse_condition(raw)


def test_catastrophic_backtracking_shape_rejected_quickly():
    start = monotonic()
    with pytest.raises(HookValidationError):
        parse_condition({"field": "message.summary", "regex": "(a+)+$"})
    assert monotonic() - start < 1


def test_long_target_unavailable_and_message_summary_truncated():
    condition = parse_condition({"field": "tool.name", "exact": "a" * 1024})
    assert not condition_matches(condition, _context(**{"tool.name": "a" * 1025}))
    summary = _context(**{"message.summary": "x" * 700})
    assert len(summary.fields["message.summary"]) == 512
    assert condition_matches(parse_condition({"field": "message.summary", "regex": "^x{256}x{256}$"}), summary)


def test_regex_step_budget_exhaustion_only_skips_match(monkeypatch):
    condition = parse_condition({"field": "message.summary", "regex": "^a?b$"})
    monkeypatch.setattr(condition_module, "MAX_REGEX_STEPS", 1)
    assert not condition_matches(condition, _context(**{"message.summary": "ab"}))
