from __future__ import annotations

from pathlib import Path

import pytest

from newcode.agent.mode import AgentMode
from newcode.context.manager import ContextManager
from newcode.permissions.manager import PermissionManager
from newcode.permissions.confirmer import DenyByDefaultConfirmer
from newcode.providers.base import ProviderError, TextDelta, ToolCallEvent
from newcode.session import ChatMessage, ChatSession
from newcode.skills.discovery import SkillDiscovery
from newcode.skills.loader import SkillLoader
from newcode.skills.runner import MAX_ISOLATED_SUMMARY_CHARACTERS, SkillRunner
from newcode.skills.state import ActiveSkillState
from newcode.skills.types import SkillValidationError
from newcode.tools.registry import create_default_registry
from newcode.tools.types import ToolCall, ToolContext
from newcode.tools.types import ToolResult, ToolSpec


class Provider:
    def __init__(self, text: str = "child result"):
        self.text = text
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), list(tools or [])))
        yield TextDelta(self.text)


class FailingProvider:
    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        raise ProviderError("safe failure")


class BatchProvider:
    def __init__(self, batches):
        self.batches = batches
        self.calls = 0

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        batch = self.batches[self.calls]
        self.calls += 1
        yield from batch


class MCPTool:
    name = "mcp__server__work__123456789abc"

    def __init__(self):
        self.calls = 0

    @property
    def spec(self):
        return ToolSpec(self.name, "mcp", {"type": "object"})

    def run(self, arguments, context):
        self.calls += 1
        return ToolResult.success(self.name)


def _activation(workspace: Path, *, history: int) -> object:
    path = workspace / ".newcode" / "skills" / "isolated.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: isolated\ndescription: Isolated work.\ntools:\n- read_file\nmode: isolated\nhistory_messages: {history}\n---\n\nisolated SOP\n",
        encoding="utf-8",
    )
    catalog = SkillDiscovery(workspace / "builtin").discover(workspace, user_home=workspace / "home")
    state = ActiveSkillState()
    state.activate(SkillLoader().load("isolated", {}, catalog, available_tools=create_default_registry().names()))
    return state.activations[0], catalog


def _runner(workspace: Path, provider, parent: ChatSession, catalog):
    context = ContextManager(parent, workspace, ("secret-value",), artifact_session_id="parent")
    return SkillRunner(
        provider=provider,
        registry=create_default_registry(),
        tool_context=ToolContext(workspace, sensitive_values=("secret-value",)),
        permission_manager=PermissionManager(),
        parent_session=parent,
        parent_context=context,
        skill_catalog=catalog,
    ), context


def test_isolated_history_zero_is_empty_and_result_is_redacted_bounded_and_reflowed(tmp_path: Path, monkeypatch):
    activation, catalog = _activation(tmp_path, history=0)
    parent = ChatSession(messages=[ChatMessage("user", "parent secret-value")])
    provider = Provider("secret-value" + "x" * (MAX_ISOLATED_SUMMARY_CHARACTERS + 20))
    cleaned: list[str] = []
    original_cleanup = ContextManager.cleanup

    def cleanup(self):
        cleaned.append(self.artifacts.session_dir.name)
        return original_cleanup(self)

    monkeypatch.setattr(ContextManager, "cleanup", cleanup)
    runner, _ = _runner(tmp_path, provider, parent, catalog)
    result = runner.run_isolated(activation, (), mode=AgentMode.DO)

    child_messages = provider.calls[0][0]
    assert result.ok is True and len(result.summary) == MAX_ISOLATED_SUMMARY_CHARACTERS
    assert "secret-value" not in result.summary
    assert [message.role for message in child_messages if message.role in ("user", "assistant")] == ["user"]
    assert parent.messages[-1].role == "assistant"
    assert result.summary in (parent.messages[-1].content or "")
    assert any(item.startswith("skill-child-") for item in cleaned)


def test_isolated_history_twenty_keeps_only_recent_redacted_non_tool_messages(tmp_path: Path):
    activation, catalog = _activation(tmp_path, history=20)
    messages = [ChatMessage("user", f"old-{index}") for index in range(25)]
    messages.extend(
        [
            ChatMessage("assistant", None, tool_calls=[ToolCall("call", "read_file", {})]),
            ChatMessage("tool", "tool secret-value output", tool_call_id="call"),
            ChatMessage("assistant", "recent secret-value"),
        ]
    )
    parent = ChatSession(messages=messages)
    provider = Provider()
    runner, _ = _runner(tmp_path, provider, parent, catalog)

    assert runner.run_isolated(activation, (), mode=AgentMode.DO).ok is True

    carried = [message.content for message in provider.calls[0][0] if message.role in ("user", "assistant")][:-1]
    assert len(carried) == 20
    assert "tool secret-value output" not in carried
    assert "recent [REDACTED]" in carried
    assert "old-5" not in carried and "old-6" in carried


def test_isolated_failure_does_not_change_parent_and_always_cleans_child(tmp_path: Path, monkeypatch):
    activation, catalog = _activation(tmp_path, history=1)
    parent = ChatSession(messages=[ChatMessage("user", "parent")])
    cleaned: list[str] = []
    monkeypatch.setattr(ContextManager, "cleanup", lambda self: cleaned.append(self.artifacts.session_dir.name) or True)
    runner, _ = _runner(tmp_path, FailingProvider(), parent, catalog)

    result = runner.run_isolated(activation, (), mode=AgentMode.DO)

    assert result.ok is False and result.code == "skill_isolated_failed"
    assert parent.messages == [ChatMessage("user", "parent")]
    assert any(item.startswith("skill-child-") for item in cleaned)


def test_optional_model_without_explicit_provider_support_fails_without_provider_fallback(tmp_path: Path):
    path = tmp_path / ".newcode" / "skills" / "model.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nname: model\ndescription: Model constrained.\ntools:\n- read_file\nmode: shared\nmodel: unavailable\n---\nSOP",
        encoding="utf-8",
    )
    catalog = SkillDiscovery(tmp_path / "builtin").discover(tmp_path, user_home=tmp_path / "home")

    with pytest.raises(SkillValidationError, match="skill_model_unavailable"):
        SkillLoader().load("model", {}, catalog, available_tools={"read_file"}, available_models=())


def test_isolated_plan_mode_keeps_mcp_hidden_and_disallowed(tmp_path: Path):
    activation, catalog = _activation(tmp_path, history=0)
    mcp = MCPTool()
    registry = create_default_registry()
    registry.register(mcp, read_only=False, do_visible=True)
    provider = Provider()
    provider.stream_chat = lambda messages, tools=None, allow_tool_calls=True: iter([ToolCallEvent([ToolCall("mcp", mcp.name, {})])])
    parent = ChatSession()
    context = ContextManager(parent, tmp_path, artifact_session_id="parent")
    runner = SkillRunner(provider=provider, registry=registry, tool_context=ToolContext(tmp_path), permission_manager=PermissionManager(), parent_session=parent, parent_context=context, skill_catalog=catalog)

    result = runner.run_isolated(activation, (), mode=AgentMode.PLAN)

    assert result.ok is False
    assert mcp.calls == 0
    assert parent.messages == []


def test_isolated_permission_denial_does_not_execute_tool_or_escape_child(tmp_path: Path):
    activation, catalog = _activation(tmp_path, history=0)
    provider = BatchProvider([[ToolCallEvent([ToolCall("read", "read_file", {"path": "missing.txt"})])], [TextDelta("safe child result")]])
    parent = ChatSession()
    context = ContextManager(parent, tmp_path, artifact_session_id="parent")
    runner = SkillRunner(
        provider=provider,
        registry=create_default_registry(),
        tool_context=ToolContext(tmp_path),
        permission_manager=PermissionManager(confirmer=DenyByDefaultConfirmer()),
        parent_session=parent,
        parent_context=context,
        skill_catalog=catalog,
    )

    result = runner.run_isolated(activation, (), mode=AgentMode.DO)

    assert result.ok is True
    assert all(message.role != "tool" for message in parent.messages)
    assert "safe child result" in (parent.messages[-1].content or "")
