from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from importlib.metadata import version

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from mcp.types import ListToolsResult


def test_mcp_sdk_version_is_the_locked_public_release() -> None:
    assert version("mcp") == "2.2.0"


def test_stdio_public_entry_accepts_explicit_process_parameters() -> None:
    parameters = StdioServerParameters(
        command="python",
        args=["local_server.py"],
        env={"LOCAL_TOKEN": "test-token"},
    )

    client_signature = inspect.signature(Client)

    assert parameters.command == "python"
    assert parameters.args == ["local_server.py"]
    assert parameters.env == {"LOCAL_TOKEN": "test-token"}
    assert "server" in client_signature.parameters
    assert client_signature.parameters["mode"].default == "auto"


def test_streamable_http_public_entry_accepts_a_header_configured_client() -> None:
    client_signature = inspect.signature(streamable_http_client)
    http_client_signature = inspect.signature(httpx2.AsyncClient)

    assert "http_client" in client_signature.parameters
    assert client_signature.parameters["http_client"].default is None
    assert "headers" in http_client_signature.parameters


def test_list_tools_public_api_supports_cursor_pagination() -> None:
    list_tools_signature = inspect.signature(Client.list_tools)
    page = ListToolsResult(tools=[], nextCursor="page-2")

    assert "cursor" in list_tools_signature.parameters
    assert page.next_cursor == "page-2"


def test_local_mcp_server_supports_success_error_and_async_cleanup() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        events.append("entered")
        try:
            yield {}
        finally:
            events.append("exited")

    async def scenario() -> None:
        server = MCPServer("compatibility-test", lifespan=lifespan)

        @server.tool()
        def add(left: int, right: int) -> int:
            return left + right

        @server.tool()
        def fail() -> str:
            raise ValueError("expected local failure")

        async with Client(server, mode="auto") as client:
            listing = await client.list_tools()
            success = await client.call_tool("add", {"left": 2, "right": 3})
            failure = await client.call_tool("fail")

        assert {tool.name for tool in listing.tools} == {"add", "fail"}
        assert success.is_error is False
        assert success.structured_content == {"result": 5}
        assert failure.is_error is True

    asyncio.run(scenario())

    assert events == ["entered", "exited"]
