"""Team 独立 YAML 配置；项目层只能缩小用户层授权。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from collections.abc import Mapping
import yaml

from .paths import canonical_directory, reject_links
from .types import Backend, MAX_MEMBERS, MAX_RUNNING_MEMBERS, TeamError


@dataclass(frozen=True)
class TeamConfig:
    enabled: bool = False
    backend_default: Backend = Backend.LIGHTWEIGHT
    max_members: int = MAX_MEMBERS
    max_running_members: int = MAX_RUNNING_MEMBERS
    require_plan_approval: bool = True
    coordinator_enabled: bool = False

    def coordinator_active(self, environment: Mapping[str, str]) -> bool:
        return self.enabled and self.coordinator_enabled and environment.get("NEWCODE_TEAM_COORDINATOR") == "1"


@dataclass(frozen=True)
class TeamConfigResult:
    config: TeamConfig
    diagnostics: tuple[tuple[str, str], ...] = ()


class _StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise TeamError("team_config_invalid")
        result[key] = loader.construct_object(value_node)
    return result


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _read(root: Path) -> dict:
    path = root / ".newcode" / "teams.yaml"
    reject_links(path, missing=True)
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 65536:
        raise TeamError("team_config_invalid")
    content = path.read_text(encoding="utf-8")
    if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(content)):
        raise TeamError("team_config_invalid")
    value = yaml.load(content, Loader=_StrictLoader)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TeamError("team_config_invalid")
    return value


def _merge(base: TeamConfig, raw: dict, *, project: bool) -> TeamConfig:
    if set(raw) - {"enabled", "backend_default", "max_members", "max_running_members", "require_plan_approval", "coordinator"}:
        raise TeamError("team_config_invalid")
    values = {}
    for key, value in raw.items():
        if key in {"enabled", "require_plan_approval"}:
            if type(value) is not bool:
                raise TeamError("team_config_invalid")
            if project and ((key == "enabled" and value) or (key == "require_plan_approval" and not value and base.require_plan_approval)):
                raise TeamError("team_config_invalid")
        elif key in {"max_members", "max_running_members"}:
            cap = MAX_MEMBERS if key == "max_members" else MAX_RUNNING_MEMBERS
            if type(value) is not int or not 1 <= value <= cap or project and value > getattr(base, key):
                raise TeamError("team_config_invalid")
        elif key == "backend_default":
            try:
                value = Backend(value)
            except (TypeError, ValueError):
                raise TeamError("team_config_invalid") from None
            if project and value is not base.backend_default:
                raise TeamError("team_config_invalid")
        elif key == "coordinator":
            if not isinstance(value, dict) or set(value) != {"enabled"} or type(value["enabled"]) is not bool:
                raise TeamError("team_config_invalid")
            value = value["enabled"]
            if project and value:
                raise TeamError("team_config_invalid")
            key = "coordinator_enabled"
        values[key] = value
    result = replace(base, **values)
    if result.max_running_members > result.max_members:
        result = replace(result, max_running_members=result.max_members)
    return result


def load_team_config(workspace_root: Path, *, user_home: Path | None = None) -> TeamConfigResult:
    config = TeamConfig()
    diagnostics = []
    for source, root in (("user", Path.home() if user_home is None else user_home), ("project", workspace_root)):
        try:
            config = _merge(config, _read(canonical_directory(root)), project=source == "project")
        except (TeamError, OSError, UnicodeError, yaml.YAMLError, RecursionError, ValueError, TypeError):
            diagnostics.append(("team_config_invalid", source))
            if source == "user":
                # 错误的用户授权不能被项目配置重新打开。
                config = TeamConfig()
    return TeamConfigResult(config, tuple(diagnostics))
