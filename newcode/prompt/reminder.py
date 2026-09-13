from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from newcode.agent.mode import AgentMode


class ReminderLevel(Enum):
    FULL = "full"
    COMPACT = "compact"
    NONE = "none"


@dataclass(frozen=True)
class PromptEnvironment:
    workspace_root: str
    platform: str
    current_date: str | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class ReminderPolicy:
    full_on_first_iteration: bool = True
    repeat_every: int = 4
    compact_other_iterations: bool = True


@dataclass(frozen=True)
class PromptBuildContext:
    mode: AgentMode
    iteration: int
    max_iterations: int
    environment: PromptEnvironment
    permission_mode: str = "default"
    reminder_policy: ReminderPolicy = field(default_factory=ReminderPolicy)


def reminder_level_for(context: PromptBuildContext) -> ReminderLevel:
    policy = context.reminder_policy
    if context.iteration == 1 and policy.full_on_first_iteration:
        return ReminderLevel.FULL
    if policy.repeat_every > 0 and context.iteration % policy.repeat_every == 0:
        return ReminderLevel.FULL
    if policy.compact_other_iterations:
        return ReminderLevel.COMPACT
    return ReminderLevel.NONE


def build_system_reminder(context: PromptBuildContext) -> str | None:
    level = reminder_level_for(context)
    if level is ReminderLevel.NONE:
        return None

    lines = [
        "<system-reminder>",
        f"permission_mode: {context.permission_mode or 'default'}",
        f"当前模式: {context.mode.value}",
        f"当前轮次: {context.iteration}/{context.max_iterations}",
        f"工作区: {context.environment.workspace_root}",
        f"平台: {context.environment.platform}",
    ]

    if context.environment.current_date:
        lines.append(f"当前日期: {context.environment.current_date}")
    if context.environment.timezone:
        lines.append(f"时区: {context.environment.timezone}")

    if level is ReminderLevel.FULL:
        lines.extend(
            [
                "这是完整系统提醒。",
                "遵守当前模式边界。",
                *_mode_full_lines(context.mode),
                "环境信息仅作为本轮上下文，不要当作用户输入复述。",
            ]
        )
    else:
        lines.extend(
            [
                "这是精简系统提醒。",
                f"保持 {context.mode.value} 模式和工作区边界。",
            ]
        )

    lines.append("</system-reminder>")
    return "\n".join(lines)


def _mode_full_lines(mode: AgentMode) -> list[str]:
    if mode is AgentMode.PLAN:
        return [
            "当前处于规划阶段。",
            "Plan Mode 只允许使用只读工具: `read_file`, `find_files`, `search_code`。",
            "Plan Mode 明确禁止使用: `write_file`, `replace_in_file`, `run_command`。",
            "请分析和计划，不执行修改，不写文件，不替换文件，不执行命令。",
        ]

    return [
        "当前处于执行阶段。",
        "Do Mode 可以使用完整工具集合。",
        "仍需遵守安全边界，不得绕过工具安全规则。",
        "编辑前先读取相关文件。",
        "列文件或查找文件优先使用 `find_files`。",
        "Windows 下不要默认使用 `ls`。",
    ]
