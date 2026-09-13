from __future__ import annotations

from newcode.mcp.adapter import MCPToolAdapter
from newcode.mcp.types import MCPServerConfig, MCPToolCallResult, MCPToolDescriptor
from newcode.agent.mode import AgentMode
from newcode.agent.scheduler import ToolScheduler
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolCall, ToolContext


class FakeManager:
    def __init__(self, result: MCPToolCallResult) -> None:
        self.result = result
        self.calls = []

    def call_tool_sync(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool, arguments))
        return self.result


def make_adapter(result: MCPToolCallResult):
    manager = FakeManager(result)
    return MCPToolAdapter(manager, MCPServerConfig(name="remote", transport="stdio", command="fake"), MCPToolDescriptor("remote", "search", "Search safely.", {"type": "object"})), manager


def test_adapter_maps_spec_and_success(tmp_path) -> None:
    adapter, manager = make_adapter(MCPToolCallResult(content=({"type": "text"},)))
    result = adapter.run({"query": "x"}, ToolContext(workspace_root=tmp_path))
    assert adapter.spec.name.startswith("mcp__remote__search__")
    assert result.ok
    assert result.metadata == {"mcp_server": "remote", "mcp_tool": "search", "transport": "stdio"}
    assert manager.calls == [("remote", "search", {"query": "x"})]


def test_adapter_maps_mcp_failure_without_unsafe_metadata(tmp_path) -> None:
    adapter, _ = make_adapter(MCPToolCallResult.failure("mcp_call_failed", "safe failure"))
    result = adapter.run({}, ToolContext(workspace_root=tmp_path))
    assert not result.ok
    assert result.error is not None and result.error.code == "mcp_call_failed"
    assert set(result.metadata) == {"mcp_server", "mcp_tool", "transport"}


def test_registry_treats_adapter_as_do_visible_side_effect() -> None:
    adapter, _ = make_adapter(MCPToolCallResult())
    registry = ToolRegistry()
    registry.register(adapter, read_only=False, do_visible=True)
    assert not registry.is_read_only(adapter.spec.name)
    assert registry.has_side_effects(adapter.spec.name)
    assert adapter.spec.name in registry.do_visible_names()
    assert ToolScheduler(registry).make_batches([ToolCall(id="1", name=adapter.spec.name)]) [0].parallel is False
    assert registry.to_openai_tools({adapter.spec.name})[0]["function"]["name"] == adapter.spec.name
