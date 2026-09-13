from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from newcode.permissions.rules import (
    PermissionRuleConfigError,
    PermissionRuleSet,
    empty_rule_set,
    parse_permission_rules_document,
)
from newcode.permissions.types import PermissionLayer


DEFAULT_USER_RULES_PATH = Path.home() / ".newcode" / "permissions.yaml"
PROJECT_RULES_RELATIVE_PATH = Path(".newcode") / "permissions.yaml"
LOCAL_PROJECT_RULES_RELATIVE_PATH = Path(".newcode") / "permissions.local.yaml"


@dataclass(frozen=True)
class PermissionRuleLoadError:
    path: Path
    source: PermissionLayer
    message: str


@dataclass(frozen=True)
class PermissionRulesLoadResult:
    user_global_rules: PermissionRuleSet
    project_rules: PermissionRuleSet
    local_project_rules: PermissionRuleSet
    errors: tuple[PermissionRuleLoadError, ...] = ()


def load_permission_rule_file(
    path: Path,
    *,
    source: PermissionLayer,
) -> tuple[PermissionRuleSet, PermissionRuleLoadError | None]:
    path = Path(path)
    if not path.exists():
        return empty_rule_set(source), None

    try:
        with path.open("r", encoding="utf-8") as file:
            raw = yaml.safe_load(file)
        return parse_permission_rules_document(raw, source=source), None
    except (OSError, PermissionRuleConfigError, yaml.YAMLError) as exc:
        return (
            empty_rule_set(source),
            PermissionRuleLoadError(
                path=path,
                source=source,
                message=str(exc),
            ),
        )


def load_permission_rules(
    workspace_root: Path,
    *,
    user_global_path: Path | None = None,
) -> PermissionRulesLoadResult:
    workspace_root = Path(workspace_root)
    user_path = user_global_path or DEFAULT_USER_RULES_PATH
    project_path = workspace_root / PROJECT_RULES_RELATIVE_PATH
    local_project_path = workspace_root / LOCAL_PROJECT_RULES_RELATIVE_PATH

    user_rules, user_error = load_permission_rule_file(
        user_path,
        source=PermissionLayer.USER_GLOBAL_RULES,
    )
    project_rules, project_error = load_permission_rule_file(
        project_path,
        source=PermissionLayer.PROJECT_RULES,
    )
    local_rules, local_error = load_permission_rule_file(
        local_project_path,
        source=PermissionLayer.LOCAL_PROJECT_RULES,
    )
    errors = tuple(
        error
        for error in (user_error, project_error, local_error)
        if error is not None
    )

    return PermissionRulesLoadResult(
        user_global_rules=user_rules,
        project_rules=project_rules,
        local_project_rules=local_rules,
        errors=errors,
    )
