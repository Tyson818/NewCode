from __future__ import annotations

from pathlib import Path

from newcode.permissions.loader import (
    LOCAL_PROJECT_RULES_RELATIVE_PATH,
    PROJECT_RULES_RELATIVE_PATH,
    load_permission_rule_file,
    load_permission_rules,
)
from newcode.permissions.types import PermissionLayer


def write_yaml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_missing_rule_file_loads_empty_rule_set(tmp_path):
    rule_set, error = load_permission_rule_file(
        tmp_path / "missing.yaml",
        source=PermissionLayer.PROJECT_RULES,
    )

    assert error is None
    assert rule_set.source is PermissionLayer.PROJECT_RULES
    assert rule_set.rules == ()


def test_loads_user_project_and_local_project_rules(tmp_path):
    workspace = tmp_path / "workspace"
    user_rules = tmp_path / "user_permissions.yaml"
    workspace.mkdir()
    write_yaml(
        user_rules,
        """
rules:
  - id: user_allow_git_status
    tool: run_command
    match:
      command: git status
    action: allow
    reason: user
    risk_level: low
""",
    )
    write_yaml(
        workspace / PROJECT_RULES_RELATIVE_PATH,
        """
rules:
  - id: project_deny_git_status
    tool: run_command
    match:
      command: git status
    action: deny
    reason: project
    risk_level: high
""",
    )
    write_yaml(
        workspace / LOCAL_PROJECT_RULES_RELATIVE_PATH,
        """
rules:
  - id: local_allow_git_status
    tool: run_command
    match:
      command: git status
    action: allow
    reason: local
    risk_level: low
""",
    )

    result = load_permission_rules(workspace, user_global_path=user_rules)

    assert result.errors == ()
    assert result.user_global_rules.rules[0].id == "user_allow_git_status"
    assert result.project_rules.rules[0].id == "project_deny_git_status"
    assert result.local_project_rules.rules[0].id == "local_allow_git_status"


def test_invalid_yaml_returns_load_error(tmp_path):
    path = tmp_path / "permissions.yaml"
    path.write_text("rules: [", encoding="utf-8")

    rule_set, error = load_permission_rule_file(
        path,
        source=PermissionLayer.PROJECT_RULES,
    )

    assert rule_set.rules == ()
    assert error is not None
    assert error.path == path
    assert error.source is PermissionLayer.PROJECT_RULES


def test_invalid_rule_returns_load_error(tmp_path):
    path = tmp_path / "permissions.yaml"
    write_yaml(
        path,
        """
rules:
  - id: ask
    tool: write_file
    match:
      path_glob: "*.txt"
    action: require_confirmation
    reason: invalid
    risk_level: medium
""",
    )

    rule_set, error = load_permission_rule_file(
        path,
        source=PermissionLayer.PROJECT_RULES,
    )

    assert rule_set.rules == ()
    assert error is not None
    assert "allow / deny" in error.message
