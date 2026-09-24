from __future__ import annotations

import subprocess

from newcode.worktrees.command_sandbox import reject_unisolated_command

from .types import (
    JsonObject,
    ToolContext,
    ToolFailure,
    ToolResult,
    ToolSpec,
    optional_positive_number,
    require_string,
)


class RunCommandTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="run_command",
            description=(
                "在工作区根目录执行非交互式本地命令，返回退出码、标准输出和标准错误。"
                "当前命令运行环境是 Windows；列目录优先使用 dir 或 PowerShell Get-ChildItem，"
                "不要默认使用 ls。"
                "如果只是列文件或查找文件，优先使用 find_files 工具，而不是 shell 命令。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令。当前是 Windows 命令环境；列目录请使用 dir 或 Get-ChildItem。",
                    },
                    "timeout_seconds": {"type": "number", "description": "命令超时时间，单位秒"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        try:
            command = require_string(arguments, "command")
            timeout = optional_positive_number(
                arguments,
                "timeout_seconds",
                context.command_timeout_seconds,
            )
        except ToolFailure as exc:
            return ToolResult.failure(self.spec.name, exc.code, exc.message, exc.details)
        if context.worktree_task_id is not None:
            # cwd/参数过滤不构成进程级文件系统隔离；没有已验证 backend 就绝不启动。
            return reject_unisolated_command(self.spec.name)
        try:
            completed = subprocess.run(
                command,
                cwd=context.cwd,
                capture_output=True,
                shell=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            return ToolResult.failure(
                self.spec.name,
                "timeout",
                "命令执行超时",
                {
                    "command": command,
                    "timeout_seconds": timeout,
                    "stdout": exc.stdout or "",
                    "stderr": exc.stderr or "",
                },
            )

        data = {
            "command": command,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        if completed.returncode != 0:
            return ToolResult.failure(
                self.spec.name,
                "command_failed",
                "命令返回非零退出码",
                data,
            )
        return ToolResult.success(self.spec.name, data)
