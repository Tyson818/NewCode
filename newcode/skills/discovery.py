"""受控本地 Skill 的三级发现与目录资源安全校验。"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable

import yaml

from .types import (
    MAX_RESOURCE_ITEMS,
    MAX_RESOURCE_METADATA_BYTES,
    MAX_SKILL_SOP_BYTES,
    SkillCatalog,
    SkillDiagnostic,
    SkillMetadata,
    SkillResource,
    SkillSource,
    SkillValidationError,
    parse_frontmatter,
)


PROJECT_SKILLS_RELATIVE_ROOT = Path(".newcode") / "skills"
USER_SKILLS_RELATIVE_ROOT = Path(".newcode") / "skills"
BUILTIN_SKILLS_RELATIVE_ROOT = Path("resources") / "skills"


class SkillDiscovery:
    """只发现和验证本地 metadata；不加载 SOP 到 prompt 或执行任何资源。"""

    def __init__(self, builtin_root: Path | None = None) -> None:
        self._builtin_root = Path(builtin_root) if builtin_root is not None else Path(__file__).parent.parent / BUILTIN_SKILLS_RELATIVE_ROOT

    def discover(
        self,
        workspace: Path,
        *,
        user_home: Path | None = None,
        builtin_root: Path | None = None,
    ) -> SkillCatalog:
        workspace_root = Path(workspace).resolve(strict=False)
        user_root = (Path(user_home) if user_home is not None else Path.home()).resolve(strict=False) / USER_SKILLS_RELATIVE_ROOT
        roots = (
            (SkillSource.PROJECT, workspace_root / PROJECT_SKILLS_RELATIVE_ROOT),
            (SkillSource.USER, user_root),
            (SkillSource.BUILTIN, Path(builtin_root) if builtin_root is not None else self._builtin_root),
        )
        winners: dict[str, SkillMetadata] = {}
        diagnostics: list[SkillDiagnostic] = []
        for source, root in roots:
            for metadata, diagnostic in self._discover_root(source, root):
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
                elif metadata is not None and metadata.frontmatter.name not in winners:
                    winners[metadata.frontmatter.name] = metadata
        return SkillCatalog(tuple(sorted(winners.values(), key=lambda item: item.frontmatter.name)), tuple(diagnostics))

    def _discover_root(self, source: SkillSource, root: Path) -> Iterable[tuple[SkillMetadata | None, SkillDiagnostic | None]]:
        if not root.exists():
            return ()
        if root.is_symlink() or not root.is_dir():
            return ((None, SkillDiagnostic("skill_discovery_failed", source)),)
        try:
            children = sorted(root.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            return ((None, SkillDiagnostic("skill_discovery_failed", source)),)
        outcomes: list[tuple[SkillMetadata | None, SkillDiagnostic | None]] = []
        for item in children:
            if item.is_symlink():
                outcomes.append((None, SkillDiagnostic("skill_resource_unsafe", source)))
                continue
            if item.is_file() and item.suffix.casefold() == ".md":
                entry = item
            elif item.is_dir() and (item / "SKILL.md").exists():
                entry = item / "SKILL.md"
            else:
                entry = None
            if entry is None:
                continue
            try:
                outcomes.append((self._metadata(source, root, entry, item if item.is_dir() else None), None))
            except SkillValidationError as exc:
                outcomes.append((None, SkillDiagnostic(exc.code, source)))
            except (OSError, UnicodeError, yaml.YAMLError):
                outcomes.append((None, SkillDiagnostic("skill_discovery_failed", source)))
        return tuple(outcomes)

    def _metadata(self, source: SkillSource, outer_root: Path, entry: Path, package_root: Path | None) -> SkillMetadata:
        if not _safe_file(entry, package_root or outer_root):
            raise SkillValidationError("skill_resource_unsafe")
        if entry.stat().st_size > MAX_SKILL_SOP_BYTES:
            raise SkillValidationError("skill_frontmatter_invalid")
        text = entry.read_text(encoding="utf-8")
        metadata = parse_frontmatter(_yaml_frontmatter(text))
        resources = _resource_index(package_root) if package_root is not None else ()
        digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        return SkillMetadata(metadata, source, package_root or outer_root, entry, digest, resources)


def _yaml_frontmatter(text: str) -> object:
    if not text.startswith("---\n"):
        raise SkillValidationError("skill_frontmatter_invalid")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SkillValidationError("skill_frontmatter_invalid")
    return yaml.safe_load(text[4:end])


def _resource_index(root: Path) -> tuple[SkillResource, ...]:
    if not _safe_directory(root, root):
        raise SkillValidationError("skill_resource_unsafe")
    resources: list[SkillResource] = []
    try:
        paths = sorted(root.rglob("*"), key=lambda path: path.as_posix())
    except OSError as exc:
        raise SkillValidationError("skill_resource_unsafe") from exc
    for path in paths:
        if path.is_symlink():
            raise SkillValidationError("skill_resource_unsafe")
        if path.is_dir():
            continue
        if not _safe_file(path, root):
            raise SkillValidationError("skill_resource_unsafe")
        relative = path.relative_to(root).as_posix()
        if relative == "SKILL.md":
            continue
        resources.append(SkillResource(relative, "file", path.stat().st_size))
    rendered = "".join(f"{item.relative_path}\0{item.kind}\0{item.size_bytes}\n" for item in resources)
    if len(resources) > MAX_RESOURCE_ITEMS or len(rendered.encode("utf-8")) > MAX_RESOURCE_METADATA_BYTES:
        raise SkillValidationError("skill_resource_unsafe")
    return tuple(resources)


def _safe_directory(path: Path, root: Path) -> bool:
    return path.exists() and path.is_dir() and not path.is_symlink() and _within_root(path, root)


def _safe_file(path: Path, root: Path) -> bool:
    return path.exists() and path.is_file() and not path.is_symlink() and _within_root(path, root)


def _within_root(path: Path, root: Path) -> bool:
    raw = Path(path).absolute()
    allowed = Path(root).resolve(strict=False)
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
