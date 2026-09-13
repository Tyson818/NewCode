from __future__ import annotations

from newcode.tools.types import JsonObject, ToolContext, ToolResult, ToolSpec

from .manager import MCPManager
from .naming import build_mcp_tool_name
from .types import MCPServerConfig, MCPToolDescriptor


class MCPToolAdapter:
    def __init__(self, manager: MCPManager, config: MCPServerConfig, descriptor: MCPToolDescriptor) -> None:
        self._manager = manager
        self._config = config
        self._descriptor = descriptor
        self._spec = ToolSpec(
            name=build_mcp_tool_name(config.name, descriptor.remote_name),
            description=f"External MCP tool: {descriptor.description}",
            parameters=descriptor.input_schema,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def mcp_metadata(self) -> JsonObject:
        return {"mcp_server": self._config.name, "mcp_tool": self._descriptor.remote_name, "transport": self._config.transport}

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        result = self._manager.call_tool_sync(self._config.name, self._descriptor.remote_name, arguments)
        if result.error is not None:
            return ToolResult.failure(self.spec.name, result.error.code, self._config.redact(result.error.message), metadata=self.mcp_metadata)
        return ToolResult.success(self.spec.name, {"content": list(result.content), "structured_content": result.structured_content}, metadata=self.mcp_metadata)
