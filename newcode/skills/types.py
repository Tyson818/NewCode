"""Skill frontmatter、发现结果与不泄露内容的诊断模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re


SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
PARAMETER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_DESCRIPTION_LENGTH = 240
MAX_SKILL_SOP_BYTES = 64 * 1024
MAX_RESOURCE_ITEMS = 200
MAX_RESOURCE_METADATA_BYTES = 1024 * 1024


class SkillMode(str, Enum):
    SHARED = "shared"
    ISOLATED = "isolated"


class SkillSource(str, Enum):
    PROJECT = "project"
    USER = "user"
    BUILTIN = "builtin"


class SkillValidationError(ValueError):
    """仅携带稳定错误码的 frontmatter 校验异常。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SkillParameter:
    name: str


@dataclass(frozen=True)
class SkillFrontmatter:
    name: str
    description: str
    tools: tuple[str, ...]
    mode: SkillMode
    history_messages: int | None = None
    model: str | None = None
    parameters: tuple[SkillParameter, ...] = ()


@dataclass(frozen=True)
class SkillResource:
    relative_path: str
    kind: str
    size_bytes: int


@dataclass(frozen=True)
class SkillMetadata:
    """仅供本地 loader 使用的元数据；绝不可直接作为模型 catalog 注入。"""

    frontmatter: SkillFrontmatter
    source: SkillSource
    root: Path
    entry: Path
    digest: str
    resources: tuple[SkillResource, ...] = ()


@dataclass(frozen=True)
class LoadedSkill:
    """只在显式加载后存在的完整 SOP 快照。"""

    metadata: SkillMetadata
    sop: str
    parameters: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SkillActivation:
    loaded: LoadedSkill
    sequence: int
    stale: bool = False


@dataclass(frozen=True)
class StartupSkillDirectoryEntry:
    """模型启动目录的最小投影，故意不包含其余 metadata。"""

    name: str
    description: str


@dataclass(frozen=True)
class SkillDiagnostic:
    code: str
    source: SkillSource
    name: str | None = None


@dataclass(frozen=True)
class SkillCatalog:
    skills: tuple[SkillMetadata, ...]
    diagnostics: tuple[SkillDiagnostic, ...] = ()

    def startup_directory(self) -> tuple[StartupSkillDirectoryEntry, ...]:
        return tuple(
            StartupSkillDirectoryEntry(
                name=metadata.frontmatter.name,
                description=metadata.frontmatter.description,
            )
            for metadata in self.skills
        )


def parse_frontmatter(value: object) -> SkillFrontmatter:
    """校验已由 YAML 解析的 frontmatter，不接受宽松或未知字段。"""

    if not isinstance(value, dict):
        raise SkillValidationError("skill_frontmatter_invalid")
    allowed = {"name", "description", "tools", "mode", "history_messages", "model", "parameters"}
    if set(value) - allowed or not {"name", "description", "tools", "mode"}.issubset(value):
        raise SkillValidationError("skill_frontmatter_invalid")

    name = _name(value["name"])
    description = _description(value["description"])
    tools = _tools(value["tools"])
    mode = _mode(value["mode"])
    history_messages = _history(value.get("history_messages"), mode)
    model = _model(value.get("model"))
    parameters = _parameters(value.get("parameters", []))
    return SkillFrontmatter(name, description, tools, mode, history_messages, model, parameters)


def _name(value: object) -> str:
    if not isinstance(value, str) or value != value.casefold() or not SKILL_NAME_PATTERN.fullmatch(value):
        raise SkillValidationError("skill_name_invalid")
    return value


def _description(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or "\n" in value
        or "\r" in value
        or len(value) > MAX_DESCRIPTION_LENGTH
    ):
        raise SkillValidationError("skill_frontmatter_invalid")
    return value


def _tools(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise SkillValidationError("skill_frontmatter_invalid")
    if len(value) != len(set(value)):
        raise SkillValidationError("skill_frontmatter_invalid")
    return tuple(value)


def _mode(value: object) -> SkillMode:
    try:
        return SkillMode(value)
    except (TypeError, ValueError) as exc:
        raise SkillValidationError("skill_frontmatter_invalid") from exc


def _history(value: object, mode: SkillMode) -> int | None:
    if mode is SkillMode.SHARED:
        if value is not None:
            raise SkillValidationError("skill_frontmatter_invalid")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 20:
        raise SkillValidationError("skill_frontmatter_invalid")
    return value


def _model(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "\n" in value or "\r" in value:
        raise SkillValidationError("skill_frontmatter_invalid")
    return value


def _parameters(value: object) -> tuple[SkillParameter, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not PARAMETER_NAME_PATTERN.fullmatch(item) for item in value):
        raise SkillValidationError("skill_frontmatter_invalid")
    if len(value) != len(set(value)):
        raise SkillValidationError("skill_frontmatter_invalid")
    return tuple(SkillParameter(item) for item in value)
