"""Worktree Phase 1 的安全错误与路径类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
from typing import Mapping
from types import MappingProxyType
from time import time


class WorktreeErrorCode(str, Enum):
    PATH_INVALID = "worktree_path_invalid"
    PATH_CONFLICT = "worktree_path_conflict"
    GIT_UNAVAILABLE = "worktree_git_unavailable"
    GIT_FAILED = "worktree_git_failed"
    GIT_TIMEOUT = "worktree_git_timeout"
    GIT_OUTPUT_INVALID = "worktree_git_output_invalid"
    CONFIG_INVALID = "worktree_config_invalid"
    LEASE_INVALID = "worktree_lease_invalid"
    OWNER_INVALID = "worktree_owner_invalid"
    REGISTRATION_INVALID = "worktree_registration_invalid"
    LOCK_TIMEOUT = "worktree_lock_timeout"
    CLEANUP_FAILED = "worktree_cleanup_failed"
    REPOSITORY_INVALID = "worktree_repository_invalid"
    SETUP_FAILED = "worktree_setup_failed"


class WorktreeState(str, Enum):
    REQUESTED = "requested"
    VALIDATING = "validating"
    CREATING = "creating"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CLEANED = "cleaned"
    PRESERVED = "preserved"
    CLEANUP_FAILED = "cleanup_failed"


class WorktreeError(ValueError):
    """仅暴露稳定错误码，不包含路径、Git 输出或配置原文。"""

    def __init__(self, code: WorktreeErrorCode | str) -> None:
        self.code = code.value if isinstance(code, WorktreeErrorCode) else str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class WorktreePath:
    repository_root: Path
    worktree_root: Path
    relative_name: str
    checkout_path: Path
    branch: str


@dataclass(frozen=True)
class WorktreeRequest:
    repository: Path
    agent_slug: str
    task_id: str


@dataclass(frozen=True)
class WorktreeLease:
    repository_root: Path
    common_git_dir: Path
    task_id: str
    agent_slug: str
    branch: str
    path: Path
    base_commit: str
    owner_token: str = field(repr=False)
    created_at: float = field(default_factory=time)
    owner_pid: int = field(default_factory=os.getpid)
    state: WorktreeState = WorktreeState.REQUESTED


@dataclass(frozen=True)
class WorktreeSetupPolicy:
    copy_files: frozenset[str] = frozenset()
    copy_ignored_files: frozenset[str] = frozenset()
    dependency_roots: Mapping[str, Path] = field(default_factory=lambda: MappingProxyType({}))
    cleanup_after_days: int = 30


@dataclass(frozen=True)
class WorktreeConfigDiagnostic:
    source: str
    code: str
