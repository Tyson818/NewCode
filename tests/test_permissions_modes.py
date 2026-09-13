from __future__ import annotations

from pathlib import Path

import pytest

from newcode.permissions.modes import (
    assess_request_risk,
    decision_for_risk,
)
from newcode.permissions.normalizer import build_permission_request
from newcode.permissions.types import (
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMode,
    PermissionRequest,
    RiskLevel,
)
from newcode.tools.types import ToolCall, ToolContext


def tool_call(name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(id="call_1", name=name, arguments=arguments, raw_arguments="{}")


def request_for(
    tmp_path,
    name: str,
    arguments: dict[str, object],
    mode: PermissionMode,
):
    return build_permission_request(
        tool_call(name, arguments),
        ToolContext(workspace_root=tmp_path),
        mode,
    )


def bare_request(mode: PermissionMode) -> PermissionRequest:
    return PermissionRequest(
        tool_name="run_command",
        original_args={},
        normalized_args={},
        workspace_root=Path.cwd(),
        mode=mode,
    )


def test_risk_assessment_marks_read_only_tools_low(tmp_path):
    assert assess_request_risk(
        request_for(tmp_path, "read_file", {"path": "README.md"}, PermissionMode.DEFAULT)
    ) is RiskLevel.LOW
    assert assess_request_risk(
        request_for(tmp_path, "find_files", {"pattern": "*.py"}, PermissionMode.DEFAULT)
    ) is RiskLevel.LOW
    assert assess_request_risk(
        request_for(tmp_path, "search_code", {"query": "PermissionManager"}, PermissionMode.DEFAULT)
    ) is RiskLevel.LOW


@pytest.mark.parametrize("tool_name", ["write_file", "replace_in_file"])
def test_risk_assessment_marks_write_tools_medium(tmp_path, tool_name):
    args: dict[str, object] = {"path": "notes.txt"}
    if tool_name == "write_file":
        args["content"] = "hello"
    else:
        args.update({"old": "a", "new": "b"})

    assert assess_request_risk(
        request_for(tmp_path, tool_name, args, PermissionMode.DEFAULT)
    ) is RiskLevel.MEDIUM


def test_risk_assessment_marks_safe_query_commands_low(tmp_path):
    assert assess_request_risk(
        request_for(tmp_path, "run_command", {"command": "git status"}, PermissionMode.DEFAULT)
    ) is RiskLevel.LOW


def test_risk_assessment_marks_general_commands_medium(tmp_path):
    assert assess_request_risk(
        request_for(tmp_path, "run_command", {"command": "python build.py"}, PermissionMode.DEFAULT)
    ) is RiskLevel.MEDIUM


def test_risk_assessment_marks_high_risk_commands_high(tmp_path):
    assert assess_request_risk(
        request_for(tmp_path, "run_command", {"command": "rm -rf build"}, PermissionMode.DEFAULT)
    ) is RiskLevel.HIGH


def test_strict_mode_does_not_silently_allow_low_or_medium_risk():
    for risk_level in (RiskLevel.LOW, RiskLevel.MEDIUM):
        decision = decision_for_risk(
            PermissionMode.STRICT,
            risk_level,
            bare_request(PermissionMode.STRICT),
        )

        assert decision.decision is PermissionDecisionValue.REQUIRE_CONFIRMATION
        assert decision.layer is PermissionLayer.PERMISSION_MODE


def test_default_mode_allows_low_confirms_medium_and_denies_high():
    low = decision_for_risk(PermissionMode.DEFAULT, RiskLevel.LOW, bare_request(PermissionMode.DEFAULT))
    medium = decision_for_risk(
        PermissionMode.DEFAULT,
        RiskLevel.MEDIUM,
        bare_request(PermissionMode.DEFAULT),
    )
    high = decision_for_risk(
        PermissionMode.DEFAULT,
        RiskLevel.HIGH,
        bare_request(PermissionMode.DEFAULT),
    )

    assert low.decision is PermissionDecisionValue.ALLOW
    assert medium.decision is PermissionDecisionValue.REQUIRE_CONFIRMATION
    assert high.decision is PermissionDecisionValue.DENY


@pytest.mark.parametrize("mode", [PermissionMode.PERMISSIVE, PermissionMode.TRUSTED])
def test_permissive_and_trusted_allow_low_medium_and_deny_high(mode):
    low = decision_for_risk(mode, RiskLevel.LOW, bare_request(mode))
    medium = decision_for_risk(mode, RiskLevel.MEDIUM, bare_request(mode))
    high = decision_for_risk(mode, RiskLevel.HIGH, bare_request(mode))

    assert low.decision is PermissionDecisionValue.ALLOW
    assert medium.decision is PermissionDecisionValue.ALLOW
    assert high.decision is PermissionDecisionValue.DENY


def test_trusted_is_permissive_alias_for_this_phase():
    for risk_level in RiskLevel:
        permissive = decision_for_risk(
            PermissionMode.PERMISSIVE,
            risk_level,
            bare_request(PermissionMode.PERMISSIVE),
        )
        trusted = decision_for_risk(
            PermissionMode.TRUSTED,
            risk_level,
            bare_request(PermissionMode.TRUSTED),
        )

        assert trusted.decision is permissive.decision
