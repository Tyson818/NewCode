"""只对受限 HookContext 求值的线性状态机条件匹配。"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from typing import Any

from .types import (
    ALLOWED_CONTEXT_FIELDS,
    MAX_MATCH_VALUE_LENGTH,
    HookCondition,
    HookContext,
    HookPredicate,
    HookValidationError,
)


MAX_REGEX_PATTERN_LENGTH = 256
MAX_REGEX_REPEAT = 256
MAX_REGEX_STATES = 4096
MAX_REGEX_STEPS = 1_000_000


@dataclass(frozen=True)
class _Atom:
    kind: str
    value: str | tuple[tuple[int, int], ...] | None = None
    minimum: int = 1
    maximum: int = 1
    negative: bool = False


def parse_condition(raw: Any) -> HookCondition:
    """验证叶子或单层 all/any；不接受表达式与递归组合。"""

    if not isinstance(raw, dict):
        raise HookValidationError("hook_condition_invalid")
    if "all" in raw or "any" in raw:
        if len(raw) != 1:
            raise HookValidationError("hook_condition_invalid")
        operator = next(iter(raw))
        items = raw[operator]
        if not isinstance(items, list) or not 1 <= len(items) <= 16:
            raise HookValidationError("hook_condition_invalid")
        return HookCondition(operator, tuple(_parse_leaf(item) for item in items))
    return HookCondition("all", (_parse_leaf(raw),))


def condition_matches(condition: HookCondition | None, context: HookContext) -> bool:
    if condition is None:
        return True
    if condition.operator not in ("all", "any"):
        return False
    checks = (_predicate_matches(item, context) for item in condition.predicates)
    return all(checks) if condition.operator == "all" else any(checks)


def _parse_leaf(raw: Any) -> HookPredicate:
    if not isinstance(raw, dict) or set(raw) - {"field", "exact", "glob", "regex", "not"}:
        raise HookValidationError("hook_condition_invalid")
    field = raw.get("field")
    if not isinstance(field, str) or field not in ALLOWED_CONTEXT_FIELDS:
        raise HookValidationError("hook_condition_invalid")
    operators = [name for name in ("exact", "glob", "regex") if name in raw]
    if len(operators) != 1 or type(raw.get("not", False)) is not bool:
        raise HookValidationError("hook_condition_invalid")
    operator = operators[0]
    pattern = raw[operator]
    if not isinstance(pattern, str) or not pattern or len(pattern) > (MAX_REGEX_PATTERN_LENGTH if operator == "regex" else MAX_MATCH_VALUE_LENGTH):
        raise HookValidationError("hook_condition_invalid")
    if operator == "regex":
        _compile_regex(pattern)
    return HookPredicate(field, operator, pattern, raw.get("not", False))


def _predicate_matches(predicate: HookPredicate, context: HookContext) -> bool:
    value = context.fields.get(predicate.field)
    if value is None:
        return False
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (str, int)):
        text = str(value)
    else:
        return False
    if len(text) > MAX_MATCH_VALUE_LENGTH:
        return False
    if predicate.operator == "exact":
        matched = text == predicate.pattern
    elif predicate.operator == "glob":
        matched = fnmatchcase(text, predicate.pattern)
    elif predicate.operator == "regex":
        matched = _regex_matches(predicate.pattern, text)
    else:
        return False
    return not matched if predicate.negate else matched


@lru_cache(maxsize=256)
def _compile_regex(pattern: str) -> tuple[_Atom, ...]:
    if not 0 < len(pattern) <= MAX_REGEX_PATTERN_LENGTH:
        raise HookValidationError("hook_condition_invalid")
    atoms: list[_Atom] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "^":
            if index != 0:
                raise HookValidationError("hook_condition_invalid")
            atoms.append(_Atom("start", minimum=0, maximum=0))
            index += 1
            continue
        if character == "$":
            if index != len(pattern) - 1:
                raise HookValidationError("hook_condition_invalid")
            atoms.append(_Atom("end", minimum=0, maximum=0))
            index += 1
            continue
        if character == "\\":
            index += 1
            if index >= len(pattern) or pattern[index].isalnum():
                raise HookValidationError("hook_condition_invalid")
            atom = _Atom("literal", pattern[index])
            index += 1
        elif character == "[":
            atom, index = _parse_class(pattern, index + 1)
        elif character == ".":
            atom = _Atom("any")
            index += 1
        elif character in "()|*+?{}]":
            raise HookValidationError("hook_condition_invalid")
        else:
            atom = _Atom("literal", character)
            index += 1
        minimum, maximum, index = _parse_quantifier(pattern, index)
        atoms.append(_Atom(atom.kind, atom.value, minimum, maximum, atom.negative))
    if sum(item.maximum for item in atoms) > MAX_REGEX_STATES:
        raise HookValidationError("hook_condition_invalid")
    return tuple(atoms)


def _parse_class(pattern: str, index: int) -> tuple[_Atom, int]:
    negative = index < len(pattern) and pattern[index] == "^"
    if negative:
        index += 1
    values: list[int] = []
    while index < len(pattern) and pattern[index] != "]":
        if pattern[index] == "\\":
            index += 1
            if index >= len(pattern) or pattern[index].isalnum():
                raise HookValidationError("hook_condition_invalid")
        values.append(ord(pattern[index]))
        index += 1
    if not values or index >= len(pattern):
        raise HookValidationError("hook_condition_invalid")
    intervals: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(values):
        if cursor + 2 < len(values) and values[cursor + 1] == ord("-"):
            if values[cursor] > values[cursor + 2]:
                raise HookValidationError("hook_condition_invalid")
            intervals.append((values[cursor], values[cursor + 2]))
            cursor += 3
        else:
            intervals.append((values[cursor], values[cursor]))
            cursor += 1
    return _Atom("class", tuple(intervals), negative=negative), index + 1


def _parse_quantifier(pattern: str, index: int) -> tuple[int, int, int]:
    if index >= len(pattern):
        return 1, 1, index
    if pattern[index] == "?":
        return 0, 1, index + 1
    if pattern[index] != "{":
        return 1, 1, index
    end = pattern.find("}", index + 1)
    if end < 0:
        raise HookValidationError("hook_condition_invalid")
    pieces = pattern[index + 1:end].split(",")
    if len(pieces) == 1:
        pieces *= 2
    if len(pieces) != 2 or any(not item or not item.isascii() or not item.isdecimal() for item in pieces):
        raise HookValidationError("hook_condition_invalid")
    minimum, maximum = (int(item) for item in pieces)
    if minimum > maximum or maximum > MAX_REGEX_REPEAT:
        raise HookValidationError("hook_condition_invalid")
    return minimum, maximum, end + 1


def _regex_matches(pattern: str, value: str) -> bool:
    """Thompson 风格状态集合；步骤预算耗尽时保守不匹配。"""

    try:
        atoms = _compile_regex(pattern)
    except HookValidationError:
        return False
    active: set[tuple[int, int]] = set()
    steps = 0
    for position in range(len(value) + 1):
        active.add((0, 0))
        pending = list(active)
        while pending:
            state = pending.pop()
            steps += 1
            if steps > MAX_REGEX_STEPS:
                return False
            index, count = state
            if index == len(atoms):
                return True
            atom = atoms[index]
            advance = (
                atom.kind == "start" and position == 0
                or atom.kind == "end" and position == len(value)
                or atom.kind not in ("start", "end") and count >= atom.minimum
            )
            if advance and (index + 1, 0) not in active:
                active.add((index + 1, 0))
                pending.append((index + 1, 0))
        if position == len(value):
            break
        following: set[tuple[int, int]] = set()
        for index, count in active:
            if index < len(atoms):
                atom = atoms[index]
                if count < atom.maximum and _atom_matches(atom, value[position]):
                    following.add((index, count + 1))
        active = following
    return False


def _atom_matches(atom: _Atom, character: str) -> bool:
    if atom.kind == "literal":
        return character == atom.value
    if atom.kind == "any":
        return character != "\n"
    if atom.kind == "class":
        ordinal = ord(character)
        inside = any(start <= ordinal <= end for start, end in atom.value or ())
        return not inside if atom.negative else inside
    return False
