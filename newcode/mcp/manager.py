from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import sys
from typing import Any

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from .runtime import MCPRuntime
from .types import (
    MCPServerConfig,
    MCPServerState,
    MCPServerStatus,
    MCPToolCallResult,
    MCPToolDescriptor,
)


@dataclass
class _ServerConnection:
    config: MCPServerConfig
    client: Any | None = None
    session: Any | None = None
    lock: asyncio.Lock | None = None
    transport_context: Any | None = None
    http_client: Any | None = None


class MCPManager:
    """在受控 runtime 中管理独立 MCP server 连接。"""

    def __init__(
        self,
        servers: Iterable[MCPServerConfig],
        *,
        runtime: MCPRuntime,
        client_factory: Callable[..., Any] = Client,
        http_client_factory: Callable[..., Any] = httpx2.AsyncClient,
        http_transport_factory: Callable[..., Any] = streamable_http_client,
    ) -> None:
        self._runtime = runtime
        self._client_factory = client_factory
        self._http_client_factory = http_client_factory
        self._http_transport_factory = http_transport_factory
        self._connections = {
            config.name: _ServerConnection(config=config)
            for config in servers
        }
        self._statuses = {
            name: MCPServerStatus(server_name=name, state=MCPServerState.PENDING)
            for name in self._connections
        }
        self._descriptors: dict[str, tuple[MCPToolDescriptor, ...]] = {}
        self._shutdown = False

    @property
    def statuses(self) -> dict[str, MCPServerStatus]:
        return dict(self._statuses)

    def discover_all(self) -> dict[str, tuple[MCPToolDescriptor, ...]]:
        discovered: dict[str, tuple[MCPToolDescriptor, ...]] = {}
        for name in self._connections:
            descriptors = self.discover_server(name)
            if self._statuses[name].error is None:
                discovered[name] = descriptors
        return discovered

    def discover_server(self, server_name: str) -> tuple[MCPToolDescriptor, ...]:
        connection = self._connections[server_name]
        if server_name in self._descriptors:
            return self._descriptors[server_name]
        if self._shutdown:
            self._mark_unavailable(connection, "mcp_server_unavailable", "MCP server is unavailable.")
            return ()
        try:
            descriptors = self._runtime.run_sync(self._discover(connection))
        except Exception:
            self._mark_unavailable(connection, "mcp_discovery_failed", "MCP discovery failed.")
            return ()
        self._descriptors[server_name] = descriptors
        self._statuses[server_name] = MCPServerStatus.ready(
            server_name,
            tool_count=len(descriptors),
        )
        return descriptors

    def call_tool_sync(
        self,
        server_name: str,
        remote_name: str,
        arguments: dict[str, Any],
    ) -> MCPToolCallResult:
        connection = self._connections.get(server_name)
        status = self._statuses.get(server_name)
        if (
            connection is None
            or self._shutdown
            or status is None
            or status.state is not MCPServerState.READY
            or connection.client is None
        ):
            return MCPToolCallResult.failure("mcp_server_unavailable", "MCP server is unavailable.")
        try:
            return self._runtime.run_sync(
                self._call_tool(connection, remote_name, arguments)
            )
        except Exception:
            self._mark_unavailable(connection, "mcp_call_failed", "MCP tool call failed.")
            return MCPToolCallResult.failure("mcp_call_failed", "MCP tool call failed.")

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        for connection in self._connections.values():
            try:
                self._runtime.run_sync(self._close_connection(connection))
            except Exception:
                pass
        self._descriptors.clear()

    async def _discover(
        self,
        connection: _ServerConnection,
    ) -> tuple[MCPToolDescriptor, ...]:
        await self._open_connection(connection)
        if connection.client is None or connection.lock is None:
            raise RuntimeError("MCP connection is unavailable.")
        tools: list[Any] = []
        cursor: str | None = None
        try:
            while True:
                async with connection.lock:
                    result = await connection.client.list_tools(cursor=cursor)
                tools.extend(result.tools)
                cursor = result.next_cursor
                if cursor is None:
                    break
        except Exception:
            await self._close_connection(connection)
            raise
        return tuple(
            MCPToolDescriptor(
                server_name=connection.config.name,
                remote_name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.input_schema),
            )
            for tool in tools
        )

    async def _call_tool(
        self,
        connection: _ServerConnection,
        remote_name: str,
        arguments: dict[str, Any],
    ) -> MCPToolCallResult:
        if connection.client is None or connection.lock is None:
            return MCPToolCallResult.failure("mcp_server_unavailable", "MCP server is unavailable.")
        async with connection.lock:
            result = await connection.client.call_tool(remote_name, arguments)
        if result.is_error:
            return MCPToolCallResult(
                content=tuple(result.content),
                structured_content=result.structured_content,
                is_error=True,
                error=MCPToolCallResult.failure("mcp_tool_error", "MCP tool returned an error.").error,
            )
        return MCPToolCallResult(
            content=tuple(result.content),
            structured_content=result.structured_content,
        )

    async def _open_connection(self, connection: _ServerConnection) -> None:
        if connection.client is not None:
            return
        if connection.config.transport == "stdio":
            parameters = StdioServerParameters(
                command=connection.config.command or "",
                args=list(connection.config.args),
                env=connection.config.env,
            )
            client = self._client_factory(parameters, mode="auto")
            await client.__aenter__()
            connection.client = client
        elif connection.config.transport == "streamable_http":
            http_client = self._http_client_factory(headers=connection.config.headers)
            transport_context = self._http_transport_factory(
                connection.config.url or "",
                http_client=http_client,
            )
            transport_entered = False
            try:
                streams = await transport_context.__aenter__()
                transport_entered = True
                client = self._client_factory(streams, mode="auto")
                await client.__aenter__()
            except Exception:
                if transport_entered:
                    await transport_context.__aexit__(*sys.exc_info())
                await http_client.aclose()
                raise
            connection.http_client = http_client
            connection.transport_context = transport_context
            connection.client = client
        else:
            raise RuntimeError("Unsupported MCP transport.")
        connection.session = connection.client.session
        connection.lock = asyncio.Lock()

    async def _close_connection(self, connection: _ServerConnection) -> None:
        errors: list[Exception] = []
        lock = connection.lock
        if lock is None:
            await self._close_connection_resources(connection, errors)
        else:
            async with lock:
                await self._close_connection_resources(connection, errors)
        if errors:
            raise errors[0]

    async def _close_connection_resources(
        self,
        connection: _ServerConnection,
        errors: list[Exception],
    ) -> None:
        client, transport_context, http_client = (
            connection.client,
            connection.transport_context,
            connection.http_client,
        )
        connection.client = None
        connection.session = None
        connection.lock = None
        connection.transport_context = None
        connection.http_client = None
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception as error:
                errors.append(error)
        if transport_context is not None:
            try:
                await transport_context.__aexit__(None, None, None)
            except Exception as error:
                errors.append(error)
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception as error:
                errors.append(error)

    def _mark_unavailable(
        self,
        connection: _ServerConnection,
        code: str,
        message: str,
    ) -> None:
        self._statuses[connection.config.name] = MCPServerStatus.unavailable(
            connection.config,
            code=code,
            message=message,
        )
