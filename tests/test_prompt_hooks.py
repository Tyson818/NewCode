"""Hook 动态背景是请求级内容，不进入会话。"""

from newcode.agent.mode import AgentMode
from newcode.prompt.builder import PromptBuilder
from newcode.prompt.modules import DynamicPromptBackground
from newcode.prompt.reminder import PromptBuildContext, PromptEnvironment
from newcode.session import ChatMessage


def test_hook_background_after_skill_before_project_and_not_persisted():
    original = [ChatMessage(role="user", content="用户原文")]
    background = DynamicPromptBackground(
        active_skills="skill SOP",
        hook_injections="hook static injection",
        project_instructions="project instructions",
    )
    context = PromptBuildContext(
        mode=AgentMode.DO, iteration=1, max_iterations=8,
        environment=PromptEnvironment(workspace_root="F:/workspace", platform="Windows"),
    )
    messages = PromptBuilder().build_messages(original, context, dynamic_background=background)
    bodies = [message.content or "" for message in messages]
    skill_index = next(index for index, body in enumerate(bodies) if "skill SOP" in body)
    hook_index = next(index for index, body in enumerate(bodies) if "hook static injection" in body)
    project_index = next(index for index, body in enumerate(bodies) if "project instructions" in body)
    assert skill_index < hook_index < project_index
    assert "非授权" in bodies[hook_index]
    assert original == [ChatMessage(role="user", content="用户原文")]
