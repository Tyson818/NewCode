"""Worktree 名称与 canonical 路径边界校验。"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from .types import WorktreeError, WorktreeErrorCode, WorktreePath


AGENT_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_RELATIVE_NAME_LENGTH = 160
MAX_GENERIC_SEGMENTS = 3
WORKTREE_ROOT_RELATIVE = Path(".newcode") / "worktrees"


def validate_relative_name(value: str, *, max_segments: int = MAX_GENERIC_SEGMENTS) -> tuple[str, ...]:
    """Validate slash-separated relative path before any Path normalization."""
    if not isinstance(value, str) or not value or len(value) > MAX_RELATIVE_NAME_LENGTH:
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
    if "\\" in value or ":" in value or value.startswith(("/", "//")):
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
    segments = tuple(value.split("/"))
    if not 1 <= len(segments) <= max_segments or any(part in {"", ".", ".."} for part in segments):
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
    if any(part.startswith("~") for part in segments):
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
    return segments


def validate_agent_slug(value: str) -> str:
    if not isinstance(value, str) or not AGENT_SLUG_PATTERN.fullmatch(value):
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
    return value


def validate_task_id(value: str) -> str:
    if not isinstance(value, str) or not TASK_ID_PATTERN.fullmatch(value):
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
    return value


def build_worktree_path(repository_root: Path, agent_slug: str, task_id: str) -> WorktreePath:
    slug = validate_agent_slug(agent_slug)
    task = validate_task_id(task_id)
    root = _canonical_directory(repository_root)
    worktree_root = root / WORKTREE_ROOT_RELATIVE
    _validate_no_reparse_components(worktree_root, root)
    canonical_worktree_root = worktree_root.resolve(strict=False)
    try:
        canonical_worktree_root.relative_to(root)
    except ValueError as exc:
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID) from exc
    relative = f"{slug}/{task}"
    segments = validate_relative_name(relative, max_segments=2)
    candidate = canonical_worktree_root.joinpath(*segments)
    _validate_no_reparse_components(candidate, canonical_worktree_root)
    try:
        candidate.resolve(strict=False).relative_to(canonical_worktree_root)
    except ValueError as exc:
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID) from exc
    if candidate.exists() or candidate.is_symlink():
        raise WorktreeError(WorktreeErrorCode.PATH_CONFLICT)
    branch = f"newcode/subagent/{task}"
    validate_branch_syntax(branch)
    return WorktreePath(root, canonical_worktree_root, relative, candidate, branch)


def validate_branch(branch: str, checker) -> str:
    """Validate local syntax and require injected fixed Git ref checker success."""
    validate_branch_syntax(branch)
    if not callable(checker):
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
    try:
        valid = checker(branch)
    except Exception as exc:
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID) from exc
    if valid is not True:
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
    return branch


def validate_branch_syntax(branch: str) -> str:
    if (
        not isinstance(branch, str)
        or not branch.startswith("newcode/subagent/")
        or len(branch) > 128
        or branch.endswith((".", ".lock", "/"))
        or ".." in branch
        or "@{" in branch
        or "\\" in branch
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in branch)
        or any(char in "~^:?*[" for char in branch)
        or any(part in {"", "."} for part in branch.split("/"))
    ):
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
    validate_task_id(branch.removeprefix("newcode/subagent/"))
    return branch


def _canonical_directory(path: Path) -> Path:
    raw = Path(path).absolute()
    try:
        mode = raw.stat().st_mode
        if not stat.S_ISDIR(mode):
            raise WorktreeError(WorktreeErrorCode.PATH_INVALID)
        _validate_no_reparse_components(raw, Path(raw.anchor))
        return raw.resolve(strict=True)
    except WorktreeError:
        raise
    except (OSError, RuntimeError) as exc:
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID) from exc


def _validate_no_reparse_components(path: Path, boundary: Path) -> None:
    raw = Path(os.path.abspath(path))
    base = Path(os.path.abspath(boundary))
    try:
        relative = raw.relative_to(base)
    except ValueError as exc:
        raise WorktreeError(WorktreeErrorCode.PATH_INVALID) from exc
    current = base
    for component in relative.parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise WorktreeError(WorktreeErrorCode.PATH_INVALID) from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            raise WorktreeError(WorktreeErrorCode.PATH_INVALID)


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)
