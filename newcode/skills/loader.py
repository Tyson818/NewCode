"""Skill 完整内容的显式、按需、安全加载。"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Iterable, Mapping

import yaml

from .types import LoadedSkill, SkillCatalog, SkillMetadata, SkillValidationError, parse_frontmatter


_PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


class SkillLoader:
    """不执行 Skill；只在调用 ``load`` 时读取完整 SOP。"""

    def load(
        self,
        name: str,
        parameters: Mapping[str, str],
        catalog: SkillCatalog,
        *,
        available_tools: Iterable[str] = (),
        available_models: Iterable[str] = (),
    ) -> LoadedSkill:
        normalized = name.casefold() if isinstance(name, str) else ""
        metadata = next((item for item in catalog.skills if item.frontmatter.name == normalized), None)
        if metadata is None:
            raise SkillValidationError("skill_not_found")
        tools = frozenset(available_tools)
        if any(tool not in tools for tool in metadata.frontmatter.tools):
            raise SkillValidationError("skill_tool_unknown")
        if metadata.frontmatter.model is not None and metadata.frontmatter.model not in frozenset(available_models):
            raise SkillValidationError("skill_model_unavailable")
        text = _read_current_entry(metadata)
        body = _body(text)
        supplied = _parameters(parameters, metadata)
        _validate_placeholders(body, set(supplied))
        sop = _PLACEHOLDER.sub(lambda match: supplied[match.group(1)], body)
        return LoadedSkill(metadata=metadata, sop=sop, parameters=tuple(sorted(supplied.items())))


def _read_current_entry(metadata: SkillMetadata) -> str:
    entry = metadata.entry
    if not _safe_entry(entry, metadata.root):
        raise SkillValidationError("skill_resource_unsafe")
    try:
        raw = entry.read_bytes()
    except OSError as exc:
        raise SkillValidationError("skill_load_failed") from exc
    if hashlib.sha256(raw).hexdigest() != metadata.digest:
        raise SkillValidationError("skill_load_failed")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillValidationError("skill_load_failed") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if parse_frontmatter(_frontmatter(text)) != metadata.frontmatter:
        raise SkillValidationError("skill_load_failed")
    return text


def _frontmatter(text: str) -> object:
    if not text.startswith("---\n"):
        raise SkillValidationError("skill_load_failed")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SkillValidationError("skill_load_failed")
    try:
        return yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        raise SkillValidationError("skill_load_failed") from exc


def _body(text: str) -> str:
    end = text.find("\n---\n", 4)
    return text[end + len("\n---\n") :].strip()


def _parameters(value: Mapping[str, str], metadata: SkillMetadata) -> dict[str, str]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise SkillValidationError("skill_parameter_invalid")
    declared = {item.name for item in metadata.frontmatter.parameters}
    supplied = set(value)
    if supplied - declared:
        raise SkillValidationError("skill_parameter_invalid")
    if declared - supplied:
        raise SkillValidationError("skill_parameter_missing")
    return dict(value)


def _validate_placeholders(body: str, declared: set[str]) -> None:
    if any(match.group(1) not in declared for match in _PLACEHOLDER.finditer(body)):
        raise SkillValidationError("skill_parameter_invalid")


def _safe_entry(entry: Path, root: Path) -> bool:
    if entry.is_symlink() or not entry.is_file():
        return False
    allowed = root.resolve(strict=False)
    raw = entry.absolute()
    try:
        raw.relative_to(allowed)
        raw.resolve(strict=False).relative_to(allowed)
    except ValueError:
        return False
    current = allowed
    for component in raw.relative_to(allowed).parts:
        current = current / component
        if current.is_symlink():
            return False
    return True
