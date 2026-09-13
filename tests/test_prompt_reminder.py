from __future__ import annotations

from newcode.agent.mode import AgentMode
from newcode.prompt import (
    PromptBuildContext,
    PromptEnvironment,
    ReminderLevel,
    ReminderPolicy,
    build_system_reminder,
    reminder_level_for,
)


def make_context(
    *,
    iteration: int = 1,
    policy: ReminderPolicy | None = None,
    permission_mode: str = "default",
) -> PromptBuildContext:
    return PromptBuildContext(
        mode=AgentMode.DO,
        iteration=iteration,
        max_iterations=8,
        environment=PromptEnvironment(
            workspace_root="C:/work/project",
            platform="Windows",
            current_date="2026-06-29",
            timezone="Asia/Shanghai",
        ),
        permission_mode=permission_mode,
        reminder_policy=policy or ReminderPolicy(),
    )


def test_prompt_environment_is_readable():
    environment = PromptEnvironment(
        workspace_root="C:/work/project",
        platform="Windows",
        current_date="2026-06-29",
        timezone="Asia/Shanghai",
    )

    assert environment.workspace_root == "C:/work/project"
    assert environment.platform == "Windows"
    assert environment.current_date == "2026-06-29"
    assert environment.timezone == "Asia/Shanghai"


def test_reminder_policy_defaults():
    policy = ReminderPolicy()

    assert policy.full_on_first_iteration is True
    assert policy.repeat_every == 4
    assert policy.compact_other_iterations is True


def test_reminder_level_enum_values():
    assert ReminderLevel.FULL.value == "full"
    assert ReminderLevel.COMPACT.value == "compact"
    assert ReminderLevel.NONE.value == "none"


def test_prompt_build_context_is_readable():
    context = make_context()

    assert context.mode is AgentMode.DO
    assert context.iteration == 1
    assert context.max_iterations == 8
    assert context.environment.workspace_root == "C:/work/project"
    assert context.permission_mode == "default"
    assert context.reminder_policy == ReminderPolicy()


def test_system_reminder_contains_tags_and_dynamic_context():
    reminder = build_system_reminder(make_context())

    assert reminder.startswith("<system-reminder>")
    assert reminder.endswith("</system-reminder>")
    assert "C:/work/project" in reminder
    assert "Windows" in reminder
    assert "do" in reminder
    assert "permission_mode: default" in reminder
    assert "1/8" in reminder
    assert "2026-06-29" in reminder
    assert "Asia/Shanghai" in reminder


def test_first_iteration_is_full_reminder():
    context = make_context(iteration=1)

    assert reminder_level_for(context) is ReminderLevel.FULL
    assert "完整系统提醒" in build_system_reminder(context)
    assert "遵守当前模式边界" in build_system_reminder(context)


def test_repeat_every_iteration_is_full_reminder():
    context = make_context(iteration=4)

    assert reminder_level_for(context) is ReminderLevel.FULL
    assert "完整系统提醒" in build_system_reminder(context)


def test_other_iterations_are_compact_reminders():
    context = make_context(iteration=2)

    assert reminder_level_for(context) is ReminderLevel.COMPACT
    reminder = build_system_reminder(context)
    assert "精简系统提醒" in reminder
    assert "当前模式: do" in reminder
    assert "permission_mode: default" in reminder
    assert "工作区: C:/work/project" in reminder


def test_system_reminder_contains_current_permission_mode():
    reminder = build_system_reminder(make_context(permission_mode="trusted"))

    assert "permission_mode: trusted" in reminder


def test_system_reminder_defaults_empty_permission_mode_to_default():
    reminder = build_system_reminder(make_context(permission_mode=""))

    assert "permission_mode: default" in reminder


def test_compact_reminder_can_be_disabled():
    context = make_context(
        iteration=2,
        policy=ReminderPolicy(compact_other_iterations=False),
    )

    assert reminder_level_for(context) is ReminderLevel.NONE
    assert build_system_reminder(context) is None


def test_reminder_without_date_or_timezone_omits_them():
    context = PromptBuildContext(
        mode=AgentMode.PLAN,
        iteration=1,
        max_iterations=3,
        environment=PromptEnvironment(
            workspace_root="C:/work/project",
            platform="Windows",
        ),
    )

    reminder = build_system_reminder(context)

    assert "当前日期" not in reminder
    assert "时区" not in reminder
    assert "plan" in reminder
