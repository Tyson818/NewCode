"""User trust ceiling 与 project Worktree 初始化请求的安全合并。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
import stat
from types import MappingProxyType
from typing import Any, Callable

import yaml

from .paths import validate_relative_name
from .types import WorktreeConfigDiagnostic, WorktreeError, WorktreeErrorCode, WorktreeSetupPolicy


USER_CONFIG_RELATIVE_PATH = Path(".newcode") / "worktree.yaml"
PROJECT_CONFIG_RELATIVE_PATH = Path(".newcode") / "worktree.yaml"
DEFAULT_CLEANUP_AFTER_DAYS = 30
MAX_CLEANUP_AFTER_DAYS = 3650

# 项目级请求只能选择这些已审阅的常见非凭据输入；用户级也不能扩展此集合。
SAFE_COPY_PATHS = frozenset(
    {
        "AGENTS.md",
        "README.md",
        ".newcode/INSTRUCTIONS.md",
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        ".python-version",
    }
)
_DEPENDENCY_ID = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_FORBIDDEN_COMPONENTS = frozenset(
    {
        ".git", ".env", ".newcode/sessions", "sessions", "memory", "context-artifacts",
        "skills-state", "cache", "caches", "logs", "log", "tmp", "temp", "secrets",
        "credentials", "keys", "certs", "certificates",
    }
)
_FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".secret", ".token", ".log", ".pyc")
_FORBIDDEN_NAME_PARTS = ("secret", "credential", "token", "apikey", "api_key", "private_key", "password")
MAX_SETUP_FILE_BYTES = 1 * 1024 * 1024
_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True)
class WorktreeConfigLoadResult:
    policy: WorktreeSetupPolicy
    diagnostics: tuple[WorktreeConfigDiagnostic, ...] = ()


@dataclass(frozen=True)
class WorktreeSetupResult:
    copied_files: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    hooks_enabled: bool = False


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in result:
                raise ValueError("duplicate_key")
        except TypeError as exc:
            raise ValueError("invalid_key") from exc
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def load_worktree_config(
    repository_root: Path,
    *,
    user_home: Path | None = None,
) -> WorktreeConfigLoadResult:
    """读取两层配置；配置错误转为安全诊断，不向上抛出原文/路径。"""
    repo = Path(repository_root).resolve(strict=False)
    home = (Path(user_home) if user_home is not None else Path.home()).resolve(strict=False)
    diagnostics: list[WorktreeConfigDiagnostic] = []
    user_raw, user_ok = _read_config(home / USER_CONFIG_RELATIVE_PATH, "user", diagnostics)
    project_raw, project_ok = _read_config(repo / PROJECT_CONFIG_RELATIVE_PATH, "project", diagnostics)
    if not user_ok:
        return WorktreeConfigLoadResult(WorktreeSetupPolicy(), tuple(diagnostics))
    user = _parse_document(user_raw, "user", diagnostics)
    if user is None:
        return WorktreeConfigLoadResult(WorktreeSetupPolicy(), tuple(diagnostics))

    user_files = _safe_items(user.get("copy_files", []), "user", diagnostics)
    user_ignored = _safe_items(user.get("copy_ignored_files", []), "user", diagnostics)
    roots = _parse_user_roots(user.get("dependency_roots", {}), diagnostics)
    user_cleanup = _cleanup_days(user.get("cleanup_after_days", DEFAULT_CLEANUP_AFTER_DAYS), "user", diagnostics)
    if project_ok:
        project = _parse_document(project_raw, "project", diagnostics)
    else:
        project = None
    if project is None:
        return WorktreeConfigLoadResult(
            WorktreeSetupPolicy(
                frozenset(), frozenset(), MappingProxyType(dict(roots)), user_cleanup
            ),
            tuple(diagnostics),
        )

    project_files = _safe_items(project.get("copy_files", []), "project", diagnostics)
    project_ignored = _safe_items(project.get("copy_ignored_files", []), "project", diagnostics)
    for _unapproved in (project_files - user_files) | (project_ignored - user_ignored):
        diagnostics.append(WorktreeConfigDiagnostic("project", "worktree_config_entry_rejected"))
    project_root_ids = _parse_project_root_ids(project.get("dependency_roots", []), diagnostics)
    for _unknown_root in project_root_ids - roots.keys():
        diagnostics.append(WorktreeConfigDiagnostic("project", "worktree_config_entry_rejected"))
    project_cleanup = _cleanup_days(project.get("cleanup_after_days", DEFAULT_CLEANUP_AFTER_DAYS), "project", diagnostics)
    return WorktreeConfigLoadResult(
        WorktreeSetupPolicy(
            copy_files=frozenset(project_files & user_files),
            copy_ignored_files=frozenset(project_ignored & user_ignored),
            dependency_roots=MappingProxyType({name: roots[name] for name in sorted(project_root_ids & roots.keys())}),
            cleanup_after_days=max(DEFAULT_CLEANUP_AFTER_DAYS, user_cleanup, project_cleanup),
        ),
        tuple(diagnostics),
    )


def initialize_worktree(
    repository_root: Path,
    worktree_root: Path,
    policy: WorktreeSetupPolicy,
    *,
    is_tracked: Callable[[str], bool] | None = None,
    is_ignored: Callable[[str], bool] | None = None,
) -> WorktreeSetupResult:
    """只复制经静态精确 allowlist 审核的普通文件；依赖链接和 hooks 保持禁用。"""

    try:
        source_root = Path(repository_root).resolve(strict=True)
        destination_root = Path(worktree_root).resolve(strict=True)
        if not source_root.is_dir() or not destination_root.is_dir():
            raise ValueError("root_not_directory")
        names = tuple(sorted(set(policy.copy_files) | set(policy.copy_ignored_files)))
        segments_by_name: list[tuple[str, tuple[str, ...]]] = []
        for name in names:
            parts = validate_relative_name(name, max_segments=16)
            normalized = "/".join(parts)
            if normalized != name or not _safe_copy_name(normalized):
                raise ValueError("copy_path_not_allowlisted")
            segments_by_name.append((normalized, parts))

        # 在任何文件写入前先验证所有来源类别；查询缺失、失败或分类不符均整体失败。
        if names and (is_tracked is None or is_ignored is None):
            raise ValueError("source_classification_unavailable")
        for name in sorted(policy.copy_files):
            if is_tracked is None or is_tracked(name) is not True:
                raise ValueError("tracked_source_required")
        for name in sorted(policy.copy_ignored_files):
            if is_ignored is None or is_ignored(name) is not True:
                raise ValueError("ignored_source_required")

        copied: list[str] = []
        for name, parts in segments_by_name:
            source = source_root.joinpath(*parts)
            destination = destination_root.joinpath(*parts)
            _validate_copy_source(source, source_root)
            _ensure_copy_destination_parent(destination.parent, destination_root)
            _copy_atomic(source, destination, destination_root)
            copied.append(name)

        diagnostics = ("worktree_dependency_link_skipped",) if policy.dependency_roots else ()
        # There is no trusted, per-worktree immutable hook install path in this phase.
        # Git Manager commands independently force their manager-owned empty hooksPath.
        return WorktreeSetupResult(tuple(copied), diagnostics, hooks_enabled=False)
    except Exception:
        raise WorktreeError(WorktreeErrorCode.SETUP_FAILED) from None


def _validate_copy_source(path: Path, boundary: Path) -> None:
    _validate_existing_chain(path, boundary, leaf_must_be_file=True)


def _ensure_copy_destination_parent(path: Path, boundary: Path) -> None:
    try:
        relative = path.absolute().relative_to(boundary.absolute())
    except ValueError as exc:
        raise ValueError("destination_escape") from exc
    current = boundary
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = current.lstat()
        if not _is_plain_directory(info):
            raise ValueError("unsafe_destination_directory")


def _validate_existing_chain(path: Path, boundary: Path, *, leaf_must_be_file: bool) -> None:
    try:
        relative = path.absolute().relative_to(boundary.absolute())
    except ValueError as exc:
        raise ValueError("path_escape") from exc
    current = boundary
    for index, part in enumerate(relative.parts):
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_FLAG:
            raise ValueError("reparse_path")
        is_leaf = index == len(relative.parts) - 1
        if is_leaf and leaf_must_be_file:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_SETUP_FILE_BYTES:
                raise ValueError("source_not_plain_file")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("parent_not_directory")


def _is_plain_directory(info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not (getattr(info, "st_file_attributes", 0) & _REPARSE_FLAG)
    )


def _copy_atomic(source: Path, destination: Path, boundary: Path) -> None:
    try:
        destination.absolute().relative_to(boundary.absolute())
    except ValueError as exc:
        raise ValueError("destination_escape") from exc
    if destination.exists() or destination.is_symlink():
        info = destination.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & _REPARSE_FLAG:
            raise ValueError("unsafe_destination_file")

    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        total = 0
        with os.fdopen(fd, "wb") as output_file:
            with source.open("rb") as input_file:
                while True:
                    chunk = input_file.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_SETUP_FILE_BYTES:
                        raise ValueError("copy_file_too_large")
                    output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_config(path: Path, source: str, diagnostics: list[WorktreeConfigDiagnostic]) -> tuple[object, bool]:
    try:
        _reject_symlink_components(path.parent)
        if not path.exists() and not path.is_symlink():
            return {}, True
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise OSError("unsafe")
        raw = path.read_bytes()
        if len(raw) > 64 * 1024:
            raise ValueError("too_large")
        return yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader), True
    except Exception:
        diagnostics.append(WorktreeConfigDiagnostic(source, "worktree_config_invalid"))
        return {}, False


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        info = current.lstat() if current.exists() or current.is_symlink() else None
        if info is not None and (stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))):
            raise OSError("unsafe")


def _parse_document(raw: object, source: str, diagnostics: list[WorktreeConfigDiagnostic]) -> dict[str, object] | None:
    if raw is None:
        raw = {}
    allowed = {"copy_files", "copy_ignored_files", "dependency_roots", "cleanup_after_days"}
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw) or set(raw) - allowed:
        diagnostics.append(WorktreeConfigDiagnostic(source, "worktree_config_invalid"))
        return None
    return raw


def _safe_items(value: object, source: str, diagnostics: list[WorktreeConfigDiagnostic]) -> set[str]:
    if not isinstance(value, list):
        diagnostics.append(WorktreeConfigDiagnostic(source, "worktree_config_invalid"))
        return set()
    accepted: set[str] = set()
    for item in value:
        try:
            segments = validate_relative_name(item, max_segments=16)
            normalized = "/".join(segments)
            if _safe_copy_name(normalized):
                accepted.add(normalized)
            else:
                diagnostics.append(WorktreeConfigDiagnostic(source, "worktree_config_entry_rejected"))
        except Exception:
            diagnostics.append(WorktreeConfigDiagnostic(source, "worktree_config_entry_rejected"))
    return accepted


def _safe_copy_name(value: str) -> bool:
    if value not in SAFE_COPY_PATHS:
        return False
    lowered_parts = tuple(part.casefold() for part in value.split("/"))
    filename = lowered_parts[-1]
    if any(part in _FORBIDDEN_COMPONENTS for part in lowered_parts):
        return False
    if filename.startswith(".env") or filename.endswith(_FORBIDDEN_SUFFIXES):
        return False
    if any(marker in filename for marker in _FORBIDDEN_NAME_PARTS):
        return False
    return True


def _parse_user_roots(value: object, diagnostics: list[WorktreeConfigDiagnostic]) -> dict[str, Path]:
    if not isinstance(value, dict):
        diagnostics.append(WorktreeConfigDiagnostic("user", "worktree_config_invalid"))
        return {}
    roots: dict[str, Path] = {}
    for name, raw_path in value.items():
        if not isinstance(name, str) or not _DEPENDENCY_ID.fullmatch(name) or not isinstance(raw_path, str):
            diagnostics.append(WorktreeConfigDiagnostic("user", "worktree_config_entry_rejected"))
            continue
        path = Path(raw_path)
        try:
            if not path.is_absolute() or path.is_symlink() or not path.is_dir():
                raise ValueError
            canonical = path.resolve(strict=True)
            if canonical != path.absolute():
                raise ValueError
            roots[name] = canonical
        except (OSError, ValueError):
            diagnostics.append(WorktreeConfigDiagnostic("user", "worktree_config_entry_rejected"))
    return roots


def _parse_project_root_ids(value: object, diagnostics: list[WorktreeConfigDiagnostic]) -> set[str]:
    if not isinstance(value, list):
        diagnostics.append(WorktreeConfigDiagnostic("project", "worktree_config_invalid"))
        return set()
    accepted: set[str] = set()
    for item in value:
        if isinstance(item, str) and _DEPENDENCY_ID.fullmatch(item):
            accepted.add(item)
        else:
            diagnostics.append(WorktreeConfigDiagnostic("project", "worktree_config_entry_rejected"))
    return accepted


def _cleanup_days(value: object, source: str, diagnostics: list[WorktreeConfigDiagnostic]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not DEFAULT_CLEANUP_AFTER_DAYS <= value <= MAX_CLEANUP_AFTER_DAYS:
        diagnostics.append(WorktreeConfigDiagnostic(source, "worktree_config_entry_rejected"))
        return DEFAULT_CLEANUP_AFTER_DAYS
    return value
