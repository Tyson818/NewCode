from __future__ import annotations

from dataclasses import dataclass

from newcode.agent import AgentLoop, AgentToolError
from newcode.agent.mode import AgentMode
from newcode.permissions.confirmer import DenyByDefaultConfirmer
from newcode.permissions.manager import PermissionManager
from newcode.permissions.types import ConfirmationResult, ConfirmationScope
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolCall, ToolContext, ToolResult, ToolSpec


class Provider:
    def __init__(self, batches): self.batches, self.calls = batches, []
    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), tools))
        yield from self.batches[len(self.calls) - 1]


@dataclass
class MCPTool:
    calls: list[str]
    name: str = "mcp__server__work__123456789abc"
    mcp_metadata = {"mcp_server": "server", "mcp_tool": "work", "transport": "stdio"}
    @property
    def spec(self): return ToolSpec(self.name, "external", {"type": "object"})
    def run(self, arguments, context):
        self.calls.append(arguments.get("order", "one"))
        return ToolResult.success(self.name, arguments)


class SessionAllow:
    def __init__(self): self.calls = 0
    def confirm(self, request, decision):
        self.calls += 1
        return ConfirmationResult(True, ConfirmationScope.SESSION, "session")


def setup(tool, provider, tmp_path, permission):
    registry = ToolRegistry(); registry.register(tool, read_only=False, do_visible=True)
    return AgentLoop(provider=provider, session=ChatSession(), registry=registry, tool_context=ToolContext(tmp_path), permission_manager=permission)


def call(id="1", **args): return ToolCall(id=id, name="mcp__server__work__123456789abc", arguments=args)


def test_mcp_deny_is_permission_denied_and_is_returned_to_next_model_turn(tmp_path):
    tool = MCPTool([]); provider = Provider([[ToolCallEvent([call()])], [TextDelta("done")]])
    loop = setup(tool, provider, tmp_path, PermissionManager(confirmer=DenyByDefaultConfirmer()))
    events = list(loop.run("x"))
    assert tool.calls == []
    assert any(isinstance(event, AgentToolError) and event.code == "permission_denied" for event in events)
    assert any(message.role == "tool" and "permission_denied" in message.content for message in provider.calls[1][0])


def test_plan_mode_rejects_mcp_without_adapter_execution(tmp_path):
    tool = MCPTool([]); provider = Provider([[ToolCallEvent([call()])]])
    events = list(setup(tool, provider, tmp_path, PermissionManager()).run("x", mode=AgentMode.PLAN))
    assert tool.calls == []
    assert any(isinstance(event, AgentToolError) and event.code == "disallowed_tool" for event in events)
    assert all(item["function"]["name"] != tool.name for item in provider.calls[0][1])


def test_mcp_session_allow_and_serial_order(tmp_path):
    confirmer = SessionAllow(); tool = MCPTool([])
    provider = Provider([
        [ToolCallEvent([call("1", order="first"), call("2", order="second")])],
        [ToolCallEvent([call("3", order="third")])],
        [TextDelta("done")],
    ])
    events = list(setup(tool, provider, tmp_path, PermissionManager(confirmer=confirmer)).run("x"))
    assert tool.calls == ["first", "second", "third"]
    assert confirmer.calls == 1
    assert [event.tool_call.id for event in events if hasattr(event, "tool_call") and event.__class__.__name__ == "AgentToolResult"] == ["1", "2", "3"]
