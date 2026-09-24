from __future__ import annotations

import subprocess

import pytest

from newcode.tools.command_tool import RunCommandTool
from newcode.tools.types import ToolContext
from newcode.worktrees.command_sandbox import WORKTREE_COMMAND_UNAVAILABLE


@pytest.mark.parametrize(
    "command",
    [
        "git status --no-optional-locks",
        "git diff",
        "git add .",
        "git commit -m unsafe",
        "git update-ref refs/heads/main HEAD",
        "type ..\\main\\secret.txt",
        "echo harmless",
    ],
)
def test_worktree_run_command_fails_closed_before_subprocess(monkeypatch, tmp_path, command):
    launched = []

    def forbidden_run(*args, **kwargs):
        launched.append((args, kwargs))
        raise AssertionError("Worktree command subprocess must not start")

    monkeypatch.setattr(subprocess, "run", forbidden_run)
    result = RunCommandTool().run(
        {"command": command},
        ToolContext(tmp_path, worktree_task_id="task-1234"),
    )

    assert not result.ok
    assert result.error.code == WORKTREE_COMMAND_UNAVAILABLE
    assert launched == []


def test_shared_run_command_keeps_existing_execution_path(tmp_path):
    # Child 专属 fail-closed 标记不得改变普通 shared workspace 的命令行为。
    result = RunCommandTool().run(
        {"command": "cmd /c exit 0"},
        ToolContext(tmp_path),
    )

    assert result.ok
