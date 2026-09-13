from __future__ import annotations

import pytest

from newcode.permissions.manager import PermissionManager
from newcode.permissions.rules import parse_permission_rules_document
from newcode.permissions.types import ConfirmationResult, ConfirmationScope, PermissionLayer, PermissionMode
from newcode.tools.types import ToolCall, ToolContext


class Tool:
    mcp_metadata = {"mcp_server": "github", "mcp_tool": "search", "transport": "stdio"}


class Allow:
    def confirm(self, request, decision):
        return ConfirmationResult(True, ConfirmationScope.SESSION, "approved")


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_unknown_mcp_requires_confirmation_in_every_mode(tmp_path, mode) -> None:
    decision = PermissionManager(mode=mode).check(ToolCall("1", "mcp__github__search__x"), ToolContext(tmp_path), Tool())
    assert decision.layer.name == "HITL_CONFIRMATION"
    assert decision.decision.name == "DENY"


def test_mcp_rule_and_session_allow_match_precise_identity(tmp_path) -> None:
    rules = parse_permission_rules_document({"rules": [{"id": "allow", "tool": "mcp__github__search__x", "match": {"mcp_server": "github", "mcp_tool_glob": "sea*"}, "action": "allow", "reason": "ok", "risk_level": "medium"}]}, source=PermissionLayer.PROJECT_RULES)
    manager = PermissionManager(project_rules=rules, confirmer=Allow())
    call = ToolCall("1", "mcp__github__search__x")
    decision = manager.check(call, ToolContext(tmp_path), Tool())
    assert decision.decision.name == "ALLOW"
    assert decision.matched_rule == "allow"
