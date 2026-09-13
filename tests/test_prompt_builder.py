from __future__ import annotations

import importlib

from newcode.agent.mode import AgentMode
from newcode.prompt import PromptBuilder, PromptModule, StablePrompt
from newcode.prompt.modules import default_optional_modules, default_stable_modules
from newcode.prompt.reminder import (
    PromptBuildContext,
    PromptEnvironment,
    ReminderPolicy,
)
from newcode.session import ChatMessage


FIXED_MODULE_KEYS = (
    "identity",
    "system_constraints",
    "task_mode",
    "action_execution",
    "tool_usage",
    "tone_style",
    "text_output",
)


def test_prompt_package_can_be_imported():
    module = importlib.import_module("newcode.prompt")

    assert module.PromptBuilder is PromptBuilder


def test_prompt_module_and_stable_prompt_are_readable():
    prompt_module = PromptModule(
        key="identity",
        title="身份",
        priority=10,
        content="你是 NewCode。",
        stable=True,
        optional=False,
    )
    stable_prompt = StablePrompt(
        content="## 身份\n你是 NewCode。",
        module_keys=("identity",),
    )

    assert prompt_module.key == "identity"
    assert prompt_module.title == "身份"
    assert prompt_module.priority == 10
    assert prompt_module.content == "你是 NewCode。"
    assert prompt_module.stable is True
    assert prompt_module.optional is False
    assert stable_prompt.content.startswith("## 身份")
    assert stable_prompt.module_keys == ("identity",)


def test_default_stable_modules_include_all_fixed_modules_in_order():
    modules = default_stable_modules()

    assert tuple(module.key for module in modules) == FIXED_MODULE_KEYS
    assert [module.priority for module in modules] == sorted(
        module.priority for module in modules
    )


def test_stable_prompt_contains_fixed_modules_in_order():
    stable_prompt = PromptBuilder().build_stable_prompt()

    assert stable_prompt.module_keys == FIXED_MODULE_KEYS
    positions = [stable_prompt.content.index(module.title) for module in default_stable_modules()]
    assert positions == sorted(positions)


def test_modules_are_separated_by_blank_lines():
    stable_prompt = PromptBuilder().build_stable_prompt()

    assert "\n\n## 系统约束" in stable_prompt.content
    assert "\n\n## 任务模式" in stable_prompt.content
    assert "\n\n## 动作执行" in stable_prompt.content


def test_identity_module_names_newcode_and_rejects_other_assistant_names():
    identity = default_stable_modules()[0]

    assert identity.title == "身份"
    assert "NewCode" in identity.content
    assert "不要自称 Claude" in identity.content
    assert "ChatGPT" in identity.content
    assert "Codex" in identity.content


def test_stable_prompt_reinforces_tool_usage_rules():
    stable_prompt = PromptBuilder().build_stable_prompt()

    for tool_name in (
        "read_file",
        "write_file",
        "replace_in_file",
        "find_files",
        "search_code",
        "run_command",
    ):
        assert tool_name in stable_prompt.content

    assert "编辑前必须先读取" in stable_prompt.content
    assert "列文件优先使用 find_files" in stable_prompt.content
    assert "Windows" in stable_prompt.content
    assert "dir" in stable_prompt.content
    assert "Get-ChildItem" in stable_prompt.content
    assert "不要默认使用 ls" in stable_prompt.content
    assert "不要伪造工具结果" in stable_prompt.content
    assert "工具失败后要根据结构化错误调整" in stable_prompt.content


def test_stable_prompt_contains_permission_system_reminder():
    stable_prompt = PromptBuilder().build_stable_prompt()

    assert "Permission System" in stable_prompt.content
    assert "权限检查" in stable_prompt.content
    assert "权限被拒绝" in stable_prompt.content
    assert "调整策略" in stable_prompt.content
    assert "更安全的操作" in stable_prompt.content
    assert "Prompt 只做提醒" in stable_prompt.content
    assert "真正的安全边界" in stable_prompt.content


def test_stable_prompt_does_not_include_dynamic_environment_information():
    stable_prompt = PromptBuilder().build_stable_prompt()

    forbidden_fragments = [
        "workspace root",
        "workspace_root",
        "current_date",
        "timezone",
        "iteration",
        "permission_mode",
        "trusted",
        "permissive",
        "当前时间",
        "当前轮次",
        "C:\\",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in stable_prompt.content


def test_default_optional_modules_are_not_rendered_when_empty():
    stable_prompt = PromptBuilder().build_stable_prompt()

    assert "自定义指令" not in stable_prompt.content
    assert "已激活 Skill" not in stable_prompt.content
    assert "长期记忆" not in stable_prompt.content
    assert stable_prompt.module_keys == FIXED_MODULE_KEYS


def test_optional_modules_with_content_can_be_appended_by_priority():
    optional_modules = default_optional_modules(
        custom_instructions="始终优先遵守项目约定。",
        active_skills="已激活：example-skill。",
        long_term_memory="用户偏好：简洁输出。",
    )

    stable_prompt = PromptBuilder(optional_modules=optional_modules).build_stable_prompt()

    assert stable_prompt.module_keys == (
        *FIXED_MODULE_KEYS,
        "custom_instructions",
        "active_skills",
        "long_term_memory",
    )
    assert "## 自定义指令\n始终优先遵守项目约定。" in stable_prompt.content
    assert "## 已激活 Skill\n已激活：example-skill。" in stable_prompt.content
    assert "## 长期记忆\n用户偏好：简洁输出。" in stable_prompt.content


def test_build_messages_adds_stable_prompt_and_reminder_before_session_messages():
    session_messages = [ChatMessage(role="user", content="你好")]
    context = PromptBuildContext(
        mode=AgentMode.DO,
        iteration=1,
        max_iterations=8,
        environment=PromptEnvironment(
            workspace_root="C:/work/project",
            platform="Windows",
            current_date="2026-06-29",
            timezone="Asia/Shanghai",
        ),
    )

    messages = PromptBuilder().build_messages(session_messages, context)

    assert [message.role for message in messages] == ["system", "system", "user"]
    assert "## 身份" in messages[0].content
    assert "<system-reminder>" in messages[1].content
    assert "C:/work/project" in messages[1].content
    assert messages[2] is session_messages[0]
    assert session_messages == [ChatMessage(role="user", content="你好")]


def test_build_messages_omits_reminder_when_policy_returns_none():
    session_messages = [ChatMessage(role="user", content="你好")]
    context = PromptBuildContext(
        mode=AgentMode.DO,
        iteration=2,
        max_iterations=8,
        environment=PromptEnvironment(
            workspace_root="C:/work/project",
            platform="Windows",
        ),
        reminder_policy=ReminderPolicy(compact_other_iterations=False),
    )

    messages = PromptBuilder().build_messages(session_messages, context)

    assert [message.role for message in messages] == ["system", "user"]
    assert "<system-reminder>" not in messages[0].content
    assert messages[1] is session_messages[0]


def test_stable_prompt_still_excludes_dynamic_context_after_message_building():
    context = PromptBuildContext(
        mode=AgentMode.DO,
        iteration=1,
        max_iterations=8,
        environment=PromptEnvironment(
            workspace_root="C:/work/project",
            platform="Windows",
            current_date="2026-06-29",
            timezone="Asia/Shanghai",
        ),
    )
    builder = PromptBuilder()

    builder.build_messages([ChatMessage(role="user", content="你好")], context)
    stable_prompt = builder.build_stable_prompt()

    assert "C:/work/project" not in stable_prompt.content
    assert "2026-06-29" not in stable_prompt.content
    assert "Asia/Shanghai" not in stable_prompt.content
    assert "iteration" not in stable_prompt.content
    assert "当前轮次" not in stable_prompt.content
