"""Worktree 子进程能力门；当前平台没有经进程级验证的 backend。"""

from __future__ import annotations

from newcode.tools.types import ToolResult


WORKTREE_COMMAND_UNAVAILABLE = "worktree_command_sandbox_unavailable"


def reject_unisolated_command(tool_name: str = "run_command") -> ToolResult:
    """未配置并验证文件系统隔离 backend 时，在进程启动前拒绝命令。"""

    return ToolResult.failure(
        tool_name,
        WORKTREE_COMMAND_UNAVAILABLE,
        "Worktree 子进程隔离能力未经验证，命令未启动。",
    )
