from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from newcode.mcp.manager import MCPManager
from newcode.mcp.runtime import MCPRuntime
from newcode.mcp.types import MCPServerConfig, MCPServerState, MCPToolCallResult


async def _thread_identifier() -> int:
    await asyncio.sleep(0)
    return threading.get_ident()


async def _raises_from_async_work() -> None:
    await asyncio.sleep(0)
    raise ValueError("expected async failure")


def test_runtime_runs_async_work_on_a_dedicated_event_loop() -> None:
    runtime = MCPRuntime()
    try:
        assert runtime.run_sync(_thread_identifier()) != threading.get_ident()
        assert runtime.run_sync(asyncio.sleep(0, result="done")) == "done"
    finally:
        runtime.shutdown()


def test_runtime_propagates_async_exceptions_and_remains_usable() -> None:
    runtime = MCPRuntime()
    try:
        with pytest.raises(ValueError, match="expected async failure"):
            runtime.run_sync(_raises_from_async_work())

        assert runtime.run_sync(asyncio.sleep(0, result="recovered")) == "recovered"
    finally:
        runtime.shutdown()


def test_runtime_shutdown_is_idempotent_and_rejects_new_work() -> None:
    runtime = MCPRuntime()

    runtime.shutdown()
    runtime.shutdown()

    assert runtime.is_closed
    with pytest.raises(RuntimeError, match="shut down"):
        runtime.run_sync(asyncio.sleep(0))


class _FakeTool:
    def __init__(self, name: str, description: str | None, input_schema: dict[str, object]) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema


class _FakeListResult:
    def __init__(self, tools: list[_FakeTool], next_cursor: str | None = None) -> None:
        self.tools = tools
        self.next_cursor = next_cursor


class _FakeCallResult:
    def __init__(
        self,
        *,
        content: list[object] | None = None,
        structured_content: object | None = None,
        is_error: bool = False,
    ) -> None:
        self.content = content or []
        self.structured_content = structured_content
        self.is_error = is_error


class _FakeClient:
    def __init__(
        self,
        parameters: object,
        *,
        mode: str,
        enter_failure: Exception | None = None,
        list_failure: Exception | None = None,
        call_failure: Exception | None = None,
        call_result: _FakeCallResult | None = None,
        call_delay: float = 0,
        pages: dict[str | None, _FakeListResult] | None = None,
    ) -> None:
        self.parameters = parameters
        self.mode = mode
        self.enter_failure = enter_failure
        self.list_failure = list_failure
        self.call_failure = call_failure
        self.call_result = call_result or _FakeCallResult(
            content=[{"type": "text", "text": "ok"}],
            structured_content={"result": "ok"},
        )
        self.call_delay = call_delay
        self.pages = pages or {
            None: _FakeListResult(
                [_FakeTool("search", "Search local data.", {"type": "object"})]
            )
        }
        self.session = object()
        self.exited = False
        self.list_cursors: list[str | None] = []
        self.call_arguments: list[tuple[str, dict[str, object]]] = []
        self.active_calls = 0
        self.max_active_calls = 0

    async def __aenter__(self) -> _FakeClient:
        if self.enter_failure is not None:
            raise self.enter_failure
        return self

    async def list_tools(self, *, cursor: str | None = None) -> _FakeListResult:
        self.list_cursors.append(cursor)
        if self.list_failure is not None:
            raise self.list_failure
        return self.pages[cursor]

    async def call_tool(self, name: str, arguments: dict[str, object]) -> _FakeCallResult:
        self.call_arguments.append((name, arguments))
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            if self.call_delay:
                await asyncio.sleep(self.call_delay)
            if self.call_failure is not None:
                raise self.call_failure
            return self.call_result
        finally:
            self.active_calls -= 1

    async def __aexit__(self, *_args: object) -> None:
        self.exited = True


class _FakeHTTPClient:
    def __init__(self, *, headers: dict[str, str], close_failure: Exception | None = None) -> None:
        self.headers = headers
        self.close_failure = close_failure
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        if self.close_failure is not None:
            raise self.close_failure


class _FakeHTTPTransport:
    def __init__(
        self,
        url: str,
        *,
        http_client: _FakeHTTPClient,
        enter_failure: Exception | None = None,
        close_failure: Exception | None = None,
    ) -> None:
        self.url = url
        self.http_client = http_client
        self.enter_failure = enter_failure
        self.close_failure = close_failure
        self.exited = False

    async def __aenter__(self) -> object:
        if self.enter_failure is not None:
            raise self.enter_failure
        return object()

    async def __aexit__(self, *_args: object) -> None:
        self.exited = True
        if self.close_failure is not None:
            raise self.close_failure


def _stdio_config(name: str) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command="local-fake-server",
        args=("--test",),
        env={"TEST_MODE": "1"},
    )


def _http_config(name: str, *, url: str = "https://local.test/mcp") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="streamable_http",
        url=url,
        headers={"Authorization": "Bearer test-secret"},
        _sensitive_values=("test-secret",),
    )


def test_manager_discovers_stdio_tools_and_maps_descriptors() -> None:
    clients: list[_FakeClient] = []

    def factory(parameters: object, *, mode: str) -> _FakeClient:
        client = _FakeClient(parameters, mode=mode)
        clients.append(client)
        return client

    runtime = MCPRuntime()
    try:
        manager = MCPManager([_stdio_config("local")], runtime=runtime, client_factory=factory)
        discovered = manager.discover_all()

        assert discovered["local"][0].remote_name == "search"
        assert discovered["local"][0].description == "Search local data."
        assert discovered["local"][0].input_schema == {"type": "object"}
        assert clients[0].mode == "auto"
        assert clients[0].parameters.command == "local-fake-server"
        assert clients[0].parameters.args == ["--test"]
        assert manager.statuses["local"].state is MCPServerState.READY
        assert manager.statuses["local"].tool_count == 1
    finally:
        manager.shutdown()
        runtime.shutdown()


@pytest.mark.parametrize(
    "failure",
    [OSError("secret-command-failed"), RuntimeError("protocol failure")],
)
def test_manager_marks_startup_or_protocol_failures_unavailable_without_details(
    failure: Exception,
) -> None:
    def factory(parameters: object, *, mode: str) -> _FakeClient:
        return _FakeClient(parameters, mode=mode, enter_failure=failure)

    runtime = MCPRuntime()
    manager = MCPManager([_stdio_config("broken")], runtime=runtime, client_factory=factory)
    try:
        assert manager.discover_all() == {}
        status = manager.statuses["broken"]
        assert status.state is MCPServerState.UNAVAILABLE
        assert status.error is not None
        assert status.error.code == "mcp_discovery_failed"
        assert "secret-command-failed" not in status.error.message
        assert "protocol failure" not in status.error.message
    finally:
        manager.shutdown()
        runtime.shutdown()


def test_manager_isolates_list_tools_failure_from_healthy_server() -> None:
    clients: dict[str, _FakeClient] = {}

    def factory(parameters: object, *, mode: str) -> _FakeClient:
        list_failure = RuntimeError("list tools failed") if parameters.command == "bad-server" else None
        client = _FakeClient(parameters, mode=mode, list_failure=list_failure)
        clients[parameters.command] = client
        return client

    healthy = _stdio_config("healthy")
    broken = MCPServerConfig(
        name="broken",
        transport="stdio",
        command="bad-server",
    )
    runtime = MCPRuntime()
    manager = MCPManager([broken, healthy], runtime=runtime, client_factory=factory)
    try:
        discovered = manager.discover_all()

        assert set(discovered) == {"healthy"}
        assert manager.statuses["broken"].state is MCPServerState.UNAVAILABLE
        assert manager.statuses["broken"].error is not None
        assert manager.statuses["broken"].error.code == "mcp_discovery_failed"
        assert clients["bad-server"].exited
        assert manager.statuses["healthy"].state is MCPServerState.READY
    finally:
        manager.shutdown()
        runtime.shutdown()


def test_manager_collects_all_public_list_tools_pages_and_reuses_cache() -> None:
    clients: list[_FakeClient] = []

    def factory(parameters: object, *, mode: str) -> _FakeClient:
        client = _FakeClient(
            parameters,
            mode=mode,
            pages={
                None: _FakeListResult(
                    [_FakeTool("first", "First page.", {"type": "object"})],
                    next_cursor="next-page",
                ),
                "next-page": _FakeListResult(
                    [_FakeTool("second", "Second page.", {"type": "object"})]
                ),
            },
        )
        clients.append(client)
        return client

    runtime = MCPRuntime()
    manager = MCPManager([_stdio_config("paged")], runtime=runtime, client_factory=factory)
    try:
        first_discovery = manager.discover_server("paged")
        second_discovery = manager.discover_server("paged")

        assert [tool.remote_name for tool in first_discovery] == ["first", "second"]
        assert second_discovery == first_discovery
        assert len(clients) == 1
        assert clients[0].list_cursors == [None, "next-page"]
    finally:
        manager.shutdown()
        assert clients[0].exited
        runtime.shutdown()


def test_manager_closes_each_stdio_context_and_shutdown_is_idempotent() -> None:
    clients: list[_FakeClient] = []

    def factory(parameters: object, *, mode: str) -> _FakeClient:
        client = _FakeClient(parameters, mode=mode)
        clients.append(client)
        return client

    runtime = MCPRuntime()
    manager = MCPManager(
        [_stdio_config("one"), _stdio_config("two")],
        runtime=runtime,
        client_factory=factory,
    )
    try:
        assert set(manager.discover_all()) == {"one", "two"}

        manager.shutdown()
        manager.shutdown()

        assert len(clients) == 2
        assert all(client.exited for client in clients)
    finally:
        runtime.shutdown()


def test_manager_discovers_http_tools_with_private_header_injection() -> None:
    http_clients: list[_FakeHTTPClient] = []
    transports: list[_FakeHTTPTransport] = []

    def http_client_factory(*, headers: dict[str, str]) -> _FakeHTTPClient:
        client = _FakeHTTPClient(headers=headers)
        http_clients.append(client)
        return client

    def transport_factory(url: str, *, http_client: _FakeHTTPClient) -> _FakeHTTPTransport:
        transport = _FakeHTTPTransport(url, http_client=http_client)
        transports.append(transport)
        return transport

    runtime = MCPRuntime()
    manager = MCPManager(
        [_http_config("remote")],
        runtime=runtime,
        client_factory=_FakeClient,
        http_client_factory=http_client_factory,
        http_transport_factory=transport_factory,
    )
    try:
        assert [tool.remote_name for tool in manager.discover_server("remote")] == ["search"]
        assert http_clients[0].headers == {"Authorization": "Bearer test-secret"}
        assert transports[0].url == "https://local.test/mcp"
        assert manager.statuses["remote"].state is MCPServerState.READY
        assert "test-secret" not in repr(manager.statuses["remote"])
    finally:
        manager.shutdown()
        assert transports[0].exited
        assert http_clients[0].closed
        runtime.shutdown()


def test_manager_isolates_http_discovery_failure_from_stdio_server() -> None:
    http_clients: list[_FakeHTTPClient] = []

    def http_client_factory(*, headers: dict[str, str]) -> _FakeHTTPClient:
        client = _FakeHTTPClient(headers=headers)
        http_clients.append(client)
        return client

    def transport_factory(url: str, *, http_client: _FakeHTTPClient) -> _FakeHTTPTransport:
        return _FakeHTTPTransport(
            url,
            http_client=http_client,
            enter_failure=RuntimeError("remote-token-failure"),
        )

    runtime = MCPRuntime()
    manager = MCPManager(
        [_http_config("broken"), _stdio_config("healthy")],
        runtime=runtime,
        client_factory=_FakeClient,
        http_client_factory=http_client_factory,
        http_transport_factory=transport_factory,
    )
    try:
        assert set(manager.discover_all()) == {"healthy"}
        status = manager.statuses["broken"]
        assert status.state is MCPServerState.UNAVAILABLE
        assert status.error is not None
        assert status.error.code == "mcp_discovery_failed"
        assert "remote-token-failure" not in status.error.message
        assert http_clients[0].closed
    finally:
        manager.shutdown()
        runtime.shutdown()


def test_manager_call_maps_success_is_error_exception_and_unavailable() -> None:
    clients: list[_FakeClient] = []

    def factory(parameters: object, *, mode: str) -> _FakeClient:
        client = _FakeClient(parameters, mode=mode)
        clients.append(client)
        return client

    runtime = MCPRuntime()
    manager = MCPManager([_stdio_config("local")], runtime=runtime, client_factory=factory)
    try:
        manager.discover_server("local")
        success = manager.call_tool_sync("local", "search", {"query": "one"})
        assert success == MCPToolCallResult(
            content=({"type": "text", "text": "ok"},),
            structured_content={"result": "ok"},
        )

        clients[0].call_result = _FakeCallResult(is_error=True, content=[{"type": "text"}])
        remote_error = manager.call_tool_sync("local", "search", {})
        assert remote_error.is_error
        assert remote_error.error is not None
        assert remote_error.error.code == "mcp_tool_error"

        clients[0].call_failure = RuntimeError("remote-secret")
        failed = manager.call_tool_sync("local", "search", {})
        assert failed.error is not None
        assert failed.error.code == "mcp_call_failed"
        assert "remote-secret" not in failed.error.message
        unavailable = manager.call_tool_sync("local", "search", {})
        assert unavailable.error is not None
        assert unavailable.error.code == "mcp_server_unavailable"
    finally:
        manager.shutdown()
        runtime.shutdown()


def test_manager_serializes_calls_for_the_same_server() -> None:
    clients: list[_FakeClient] = []

    def factory(parameters: object, *, mode: str) -> _FakeClient:
        client = _FakeClient(parameters, mode=mode, call_delay=0.02)
        clients.append(client)
        return client

    runtime = MCPRuntime()
    manager = MCPManager([_stdio_config("local")], runtime=runtime, client_factory=factory)
    try:
        manager.discover_server("local")
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda index: manager.call_tool_sync("local", "search", {"index": index}),
                    range(2),
                )
            )

        assert all(not result.is_error for result in results)
        assert clients[0].max_active_calls == 1
        assert len(clients[0].call_arguments) == 2
    finally:
        manager.shutdown()
        runtime.shutdown()


def test_shutdown_continues_after_one_connection_cleanup_failure() -> None:
    clients: dict[str, _FakeClient] = {}

    def factory(parameters: object, *, mode: str) -> _FakeClient:
        failure = RuntimeError("close failed") if parameters.command == "bad-close" else None
        client = _FakeClient(parameters, mode=mode)
        if failure is not None:
            async def failing_exit(*_args: object) -> None:
                client.exited = True
                raise failure

            client.__aexit__ = failing_exit
        clients[parameters.command] = client
        return client

    bad = MCPServerConfig(name="bad", transport="stdio", command="bad-close")
    good = MCPServerConfig(name="good", transport="stdio", command="good-close")
    runtime = MCPRuntime()
    manager = MCPManager([bad, good], runtime=runtime, client_factory=factory)
    try:
        assert set(manager.discover_all()) == {"bad", "good"}
        manager.shutdown()
        manager.shutdown()

        assert clients["bad-close"].exited
        assert clients["good-close"].exited
    finally:
        runtime.shutdown()
