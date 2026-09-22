from __future__ import annotations

from newcode.agent.mode import AgentMode
from newcode.prompt.builder import PromptBuilder
from newcode.prompt.modules import DynamicPromptBackground
from newcode.prompt.reminder import PromptBuildContext, PromptEnvironment
from newcode.session import ChatMessage


def _context() -> PromptBuildContext:
    return PromptBuildContext(
        mode=AgentMode.DO,
        iteration=1,
        max_iterations=8,
        environment=PromptEnvironment(workspace_root="C:/work", platform="Windows"),
    )


def test_dynamic_background_has_locked_order_and_does_not_mutate_session():
    session_messages = [ChatMessage(role="user", content="original user text")]
    messages = PromptBuilder().build_messages(
        session_messages,
        _context(),
        dynamic_background=DynamicPromptBackground(
            project_instructions="project",
            workspace_instructions="workspace",
            user_instructions="user",
            memory="memory scope project",
        ),
    )

    contents = [message.content or "" for message in messages]
    assert [message.role for message in messages] == ["system"] * 6 + ["user"]
    assert "项目指令" in contents[1] and contents[1].endswith("project")
    assert "工作区指令" in contents[2] and contents[2].endswith("workspace")
    assert "用户指令" in contents[3] and contents[3].endswith("user")
    assert "已筛选记忆" in contents[4] and "scope：user/project" in contents[4]
    assert "不授予权限" in contents[1]
    assert "<system-reminder>" in contents[5]
    assert messages[-1] is session_messages[0]
    assert session_messages == [ChatMessage(role="user", content="original user text")]


def test_empty_dynamic_background_preserves_existing_message_order():
    messages = PromptBuilder().build_messages(
        [ChatMessage(role="user", content="original")],
        _context(),
        dynamic_background=DynamicPromptBackground(),
    )

    assert [message.role for message in messages] == ["system", "system", "user"]
