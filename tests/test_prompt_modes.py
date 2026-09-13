from __future__ import annotations

from newcode.agent.mode import AgentMode
from newcode.prompt import PromptBuildContext, PromptEnvironment, build_system_reminder


def make_context(mode: AgentMode) -> PromptBuildContext:
    return PromptBuildContext(
        mode=mode,
        iteration=1,
        max_iterations=8,
        environment=PromptEnvironment(
            workspace_root="C:/work/project",
            platform="Windows",
            current_date="2026-06-29",
            timezone="Asia/Shanghai",
        ),
    )


def test_plan_mode_reminder_contains_read_only_tools():
    reminder = build_system_reminder(make_context(AgentMode.PLAN))

    assert "read_file" in reminder
    assert "find_files" in reminder
    assert "search_code" in reminder


def test_plan_mode_reminder_contains_disallowed_tools():
    reminder = build_system_reminder(make_context(AgentMode.PLAN))

    assert "write_file" in reminder
    assert "replace_in_file" in reminder
    assert "run_command" in reminder
    assert "禁止" in reminder


def test_plan_mode_reminder_describes_planning_without_execution():
    reminder = build_system_reminder(make_context(AgentMode.PLAN))

    assert "规划阶段" in reminder
    assert "分析和计划" in reminder
    assert "不执行修改" in reminder
    assert "不写文件" in reminder
    assert "不执行命令" in reminder
    assert "可以写文件" not in reminder
    assert "可以执行命令" not in reminder


def test_do_mode_reminder_describes_execution_stage_and_full_tools():
    reminder = build_system_reminder(make_context(AgentMode.DO))

    assert "执行阶段" in reminder
    assert "完整工具集合" in reminder


def test_do_mode_reminder_keeps_safety_boundaries():
    reminder = build_system_reminder(make_context(AgentMode.DO))

    assert "安全边界" in reminder
    assert "不得绕过工具安全规则" in reminder


def test_do_mode_reminder_reinforces_tool_choice_rules():
    reminder = build_system_reminder(make_context(AgentMode.DO))

    assert "编辑前先读取" in reminder
    assert "find_files" in reminder
    assert "Windows" in reminder
    assert "不要默认使用 `ls`" in reminder
