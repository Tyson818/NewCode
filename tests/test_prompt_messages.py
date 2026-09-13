from __future__ import annotations

from newcode.agent import AgentLoop, AgentLoopConfig
from newcode.agent.mode import AgentMode
from newcode.permissions.manager import PermissionManager
from newcode.permissions.types import PermissionMode
from newcode.prompt import PromptBuilder, PromptBuildContext, PromptEnvironment, ReminderPolicy
from newcode.providers.base import TextDelta
from newcode.session import ChatMessage, ChatSession
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolContext
from newcode.tools.types import ToolCall


class FakeProvider:
    def __init__(self):
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "allow_tool_calls": allow_tool_calls,
            }
        )
        yield TextDelta("ok")


class SpyPromptBuilder:
    def __init__(self):
        self.contexts = []

    def build_messages(self, session_messages, context):
        self.contexts.append(context)
        return list(session_messages)


def make_context(
    *,
    iteration: int = 1,
    compact_other_iterations: bool = True,
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
        reminder_policy=ReminderPolicy(
            compact_other_iterations=compact_other_iterations,
        ),
    )


def test_build_messages_orders_stable_prompt_reminder_and_session_messages():
    session_messages = [
        ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="hi"),
    ]

    messages = PromptBuilder().build_messages(session_messages, make_context())

    assert [message.role for message in messages] == [
        "system",
        "system",
        "user",
        "assistant",
    ]
    assert "NewCode" in messages[0].content
    assert "<system-reminder>" in messages[1].content
    assert "C:/work/project" in messages[1].content
    assert messages[2:] == session_messages


def test_build_messages_does_not_modify_session_messages():
    session_messages = [ChatMessage(role="user", content="hello")]
    original = list(session_messages)

    messages = PromptBuilder().build_messages(session_messages, make_context())

    assert session_messages == original
    assert len(session_messages) == 1
    assert messages[0] not in session_messages
    assert messages[1] not in session_messages


def test_build_messages_omits_empty_reminder_when_policy_returns_none():
    session_messages = [ChatMessage(role="user", content="hello")]
    context = make_context(iteration=2, compact_other_iterations=False)

    messages = PromptBuilder().build_messages(session_messages, context)

    assert [message.role for message in messages] == ["system", "user"]
    assert "NewCode" in messages[0].content
    assert "<system-reminder>" not in messages[0].content
    assert messages[1:] == session_messages


def test_system_prompt_and_reminder_are_not_written_to_session_object():
    session_messages = [ChatMessage(role="user", content="hello")]

    PromptBuilder().build_messages(session_messages, make_context())

    assert [message.role for message in session_messages] == ["user"]
    assert all("<system-reminder>" not in (message.content or "") for message in session_messages)
    assert all("NewCode" not in (message.content or "") for message in session_messages)


def test_build_messages_preserves_tool_messages_after_system_messages():
    tool_call = ToolCall(
        id="call_1",
        name="read_file",
        arguments={"path": "README.md"},
        raw_arguments='{"path": "README.md"}',
    )
    session_messages = [
        ChatMessage(role="assistant", content=None, tool_calls=[tool_call]),
        ChatMessage(role="tool", content="{}", tool_call_id="call_1"),
    ]

    messages = PromptBuilder().build_messages(session_messages, make_context())

    assert messages[2].tool_calls == [tool_call]
    assert messages[3].role == "tool"
    assert messages[3].tool_call_id == "call_1"


def test_agent_loop_passes_permission_mode_to_prompt_context(tmp_path):
    provider = FakeProvider()
    prompt_builder = SpyPromptBuilder()
    loop = AgentLoop(
        provider=provider,
        session=ChatSession(),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        config=AgentLoopConfig(max_iterations=1),
        prompt_builder=prompt_builder,
        permission_manager=PermissionManager(mode=PermissionMode.TRUSTED),
    )

    list(loop.run("hello"))

    assert prompt_builder.contexts[0].permission_mode == "trusted"
