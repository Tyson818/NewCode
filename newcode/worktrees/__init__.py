"""受限 Git Worktree 基础组件；本包不向子 Agent 暴露 Git 写操作。"""

from .paths import WorktreePath, build_worktree_path, validate_branch, validate_relative_name
from .types import WorktreeError, WorktreeErrorCode

__all__ = [
    "WorktreeError",
    "WorktreeErrorCode",
    "WorktreePath",
    "build_worktree_path",
    "validate_branch",
    "validate_relative_name",
]
