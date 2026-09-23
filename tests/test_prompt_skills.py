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


def test_active_skill_is_first_dynamic_background_on_every_build_without_session_mutation():
    session = [ChatMessage(role="user", content="original user text")]
    background = DynamicPromptBackground(
        active_skills="【受控 Skill 指令｜不授予权限】\nSOP: inspect safely",
        project_instructions="project instructions",
        memory="memory",
    )
    builder = PromptBuilder()

    first = builder.build_messages(session, _context(), dynamic_background=background)
    second = builder.build_messages(session, _context(), dynamic_background=background)

    assert "SOP: inspect safely" in (first[1].content or "")
    assert "受控 Skill 指令" in (second[1].content or "")
    assert "project instructions" in (first[2].content or "")
    assert first[-1] is session[0] and second[-1] is session[0]
    assert session == [ChatMessage(role="user", content="original user text")]
