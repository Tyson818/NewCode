from __future__ import annotations

from pathlib import Path

from newcode.agent import AgentLoop, AgentToolError, AgentToolResult
from newcode.agent.mode import AgentMode
from newcode.permissions.confirmer import DenyByDefaultConfirmer
from newcode.permissions.manager import PermissionManager
from newcode.permissions.types import ConfirmationResult, ConfirmationScope
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.skills.state import ActiveSkillState
from newcode.skills.policy import visible_tool_names
from newcode.skills.discovery import SkillDiscovery
from newcode.skills.loader import SkillLoader
from newcode.skills.tool import LoadSkillTool
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolCall, ToolContext, ToolResult, ToolSpec


class Provider:
    def __init__(self, batches):
        self.batches = batches
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), list(tools or []), allow_tool_calls))
        yield from self.batches[len(self.calls) - 1]


class AllowConfirmer:
    def __init__(self):
        self.calls = 0

    def confirm(self, request, decision):
        self.calls += 1
        return ConfirmationResult(True, ConfirmationScope.ONCE, "allowed")


class RecordingTool:
    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    @property
    def spec(self):
        return ToolSpec(self.name, "fake", {"type": "object"})

    def run(self, arguments, context):
        self.calls += 1
        return ToolResult.success(self.name, arguments)


def _skill(workspace: Path, *, name: str = "demo", tools: tuple[str, ...] = ("read_file",)) -> None:
    path = workspace / ".newcode" / "skills" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered_tools = "\n".join(f"- {tool}" for tool in tools)
    path.write_text(
        f"---\nname: {name}\ndescription: {name} description.\ntools:\n{rendered_tools}\nmode: shared\n---\n\n{name} complete SOP\n",
        encoding="utf-8",
    )


def _tool_call(name: str, call_id: str = "load") -> ToolCall:
    return ToolCall(call_id, name, {"name": "demo", "parameters": {}})


def _loop(provider, workspace: Path, permission, registry: ToolRegistry | None = None, state=None):
    return AgentLoop(
        provider=provider,
        session=ChatSession(),
        registry=registry or ToolRegistry(),
        tool_context=ToolContext(workspace),
        permission_manager=permission,
        skill_state=state,
    )


def test_load_skill_is_visible_in_plan_and_do_and_is_serial(tmp_path: Path):
    _skill(tmp_path)
    for mode in (AgentMode.PLAN, AgentMode.DO):
        provider = Provider([[TextDelta("done")]])
        loop = _loop(provider, tmp_path, PermissionManager(confirmer=AllowConfirmer()))

        events = list(loop.run("check", mode=mode))

        names = {item["function"]["name"] for item in provider.calls[0][1]}
        assert "load_skill" in names
        assert loop.scheduler.make_batches([ToolCall("1", "load_skill", {})])[0].parallel is False
        assert events


def test_load_skill_follows_permission_deny_and_confirmation_before_activation(tmp_path: Path):
    _skill(tmp_path)
    denied_state = ActiveSkillState()
    denied = _loop(
        Provider([[ToolCallEvent([_tool_call("load_skill")])], [TextDelta("done")]]),
        tmp_path,
        PermissionManager(confirmer=DenyByDefaultConfirmer()),
        state=denied_state,
    )

    denied_events = list(denied.run("load", mode=AgentMode.PLAN))
    assert any(isinstance(item, AgentToolError) and item.code == "permission_denied" for item in denied_events)
    assert denied_state.activations == ()

    confirmer = AllowConfirmer()
    allowed_state = ActiveSkillState()
    allowed_registry = ToolRegistry()
    allowed_registry.register(RecordingTool("read_file"), read_only=True, do_visible=True)
    allowed = _loop(
        Provider([[ToolCallEvent([_tool_call("load_skill")])], [TextDelta("done")]]),
        tmp_path,
        PermissionManager(confirmer=confirmer),
        registry=allowed_registry,
        state=allowed_state,
    )
    allowed_events = list(allowed.run("load", mode=AgentMode.DO))

    assert confirmer.calls == 1
    assert any(isinstance(item, AgentToolResult) and item.tool_call.name == "load_skill" for item in allowed_events)
    assert [item.loaded.metadata.frontmatter.name for item in allowed_state.activations] == ["demo"]


def test_successful_load_narrows_next_request_and_injects_sop(tmp_path: Path):
    _skill(tmp_path, tools=("read_file",))
    mcp = RecordingTool("mcp__server__work__123456789abc")
    registry = ToolRegistry()
    registry.register(RecordingTool("read_file"), read_only=True, do_visible=True)
    registry.register(mcp, read_only=False, do_visible=True)
    provider = Provider(
        [
            [ToolCallEvent([_tool_call("load_skill")])],
            [ToolCallEvent([ToolCall("mcp", mcp.name, {})])],
        ]
    )
    loop = _loop(provider, tmp_path, PermissionManager(confirmer=AllowConfirmer()), registry=registry)

    events = list(loop.run("load"))

    second_names = {item["function"]["name"] for item in provider.calls[1][1]}
    assert second_names == {"read_file", "load_skill"}
    assert "demo complete SOP" in (provider.calls[1][0][1].content or "")
    assert mcp.calls == 0
    assert any(isinstance(item, AgentToolError) and item.code == "disallowed_tool" for item in events)


def test_unknown_whitelist_tool_fails_before_activation_and_load_skill_remains_visible(tmp_path: Path):
    _skill(tmp_path, tools=("missing_tool",))
    state = ActiveSkillState()
    provider = Provider([[ToolCallEvent([_tool_call("load_skill")])], [TextDelta("done")]])

    events = list(_loop(provider, tmp_path, PermissionManager(confirmer=AllowConfirmer()), state=state).run("load"))

    assert state.activations == ()
    assert any(isinstance(item, AgentToolError) and item.code == "skill_tool_unknown" for item in events)


def test_multiple_active_whitelists_can_have_empty_regular_intersection(tmp_path: Path):
    _skill(tmp_path, name="reader", tools=("read_file",))
    _skill(tmp_path, name="writer", tools=("write_file",))
    registry = ToolRegistry()
    registry.register(RecordingTool("read_file"), read_only=True, do_visible=True)
    registry.register(RecordingTool("write_file"), read_only=False, do_visible=True)
    state = ActiveSkillState()
    catalog = SkillDiscovery(tmp_path / "builtin").discover(tmp_path, user_home=tmp_path / "home")
    loader = SkillLoader()
    state.activate(loader.load("reader", {}, catalog, available_tools=registry.names()))
    state.activate(loader.load("writer", {}, catalog, available_tools=registry.names()))
    registry.register(LoadSkillTool(catalog=lambda: catalog, state=state, registry=registry), read_only=False, do_visible=True)

    assert visible_tool_names(AgentMode.DO, registry, state) == frozenset({"load_skill"})
