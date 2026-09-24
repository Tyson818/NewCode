"""Chapter 12 CLI Hook 生命周期与资源责任方回归。"""

from __future__ import annotations

from io import BytesIO, StringIO
from pathlib import Path
import ssl
from types import SimpleNamespace

import pytest

from newcode import cli
from newcode.hooks.loader import load_hook_rules
from newcode.hooks.types import (
    HookAction, HookActionType, HookEvent, HookLoadResult,
    HookNetworkPolicy, HookRule, HookSource,
)
from newcode.mcp.config import MCPConfigLoadResult
from newcode.persistence import SessionArchive
from newcode.providers.base import TextDelta
from newcode.session import ChatSession
from newcode.tools.types import ToolContext


def _inputs(*values):
    remaining = iter(values)

    def read(_prompt):
        value = next(remaining)
        if isinstance(value, BaseException):
            raise value
        return value

    return read


def _rules(*events: HookEvent) -> HookLoadResult:
    rules = tuple(
        HookRule(
            f"rule_{index}", event,
            HookAction(HookActionType.PROMPT_INJECTION, {"text": f"MARKER_{index}"}),
            HookSource.USER,
            once=True,
        )
        for index, event in enumerate(events)
    )
    return HookLoadResult(rules, HookNetworkPolicy())


class Provider:
    def __init__(self):
        self.messages = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.messages.append(tuple(messages))
        yield TextDelta("answer")


def test_no_hook_config_keeps_cli_behavior(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cli, "load_hook_rules", lambda _root: HookLoadResult((), HookNetworkPolicy()))
    output = StringIO()
    result = cli.run_conversation(
        Provider(), ChatSession(), tool_context=ToolContext(tmp_path),
        input_func=_inputs("/plan", "/do", "/exit"), output=output,
    )
    assert result == 0
    assert "NewCode 已启动" in output.getvalue()


def test_hook_loader_internal_failure_is_safe_and_does_not_stop_cli(monkeypatch, tmp_path: Path):
    def broken(_root):
        raise RuntimeError("secret-token")

    monkeypatch.setattr(cli, "load_hook_rules", broken)
    errors = StringIO()
    assert cli.run_conversation(
        Provider(), ChatSession(), tool_context=ToolContext(tmp_path),
        input_func=_inputs("/exit"), output=StringIO(), error_output=errors,
    ) == 0
    assert "hook_config_invalid" in errors.getvalue()
    assert "secret-token" not in errors.getvalue()


def test_cli_loads_user_project_and_isolates_bad_rule_without_secret(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    for root in (home, workspace):
        (root / ".newcode").mkdir(parents=True)
    (home / ".newcode" / "hooks.yaml").write_text(
        "network:\n  enabled: true\n  allow_hosts: [example.com]\n"
        "hooks:\n  - {id: user_start, event: system_start, action: {type: prompt_injection, text: USER_MARK}}\n",
        encoding="utf-8",
    )
    (workspace / ".newcode" / "hooks.yaml").write_text(
        "network:\n  enabled: true\n  allow_hosts: [evil.example]\n"
        "hooks:\n"
        "  - {id: bad, event: secret-token, action: {type: subagent}}\n"
        "  - {id: project_start, event: session_start, action: {type: prompt_injection, text: PROJECT_MARK}}\n",
        encoding="utf-8",
    )
    loaded = load_hook_rules(workspace, user_home=home)
    assert [rule.id for rule in loaded.rules] == ["user_start", "project_start"]
    assert loaded.network.allow_hosts == ("example.com",)
    monkeypatch.setattr(cli, "load_hook_rules", lambda _root: loaded)
    provider = Provider()
    errors = StringIO()
    cli.run_conversation(
        provider, ChatSession(), tool_context=ToolContext(workspace),
        input_func=_inputs("hello", "/exit"), output=StringIO(), error_output=errors,
    )
    assert "secret-token" not in errors.getvalue()
    assert "hook_event_invalid" in errors.getvalue()
    assert "USER_MARK" in str(provider.messages[0])
    assert "PROJECT_MARK" in str(provider.messages[0])


def test_cli_injects_production_http_transport_only_for_authorized_config(monkeypatch, tmp_path: Path):
    import newcode.hooks.actions as hook_actions

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    (home / ".newcode").mkdir(parents=True)
    (workspace / ".newcode").mkdir(parents=True)
    user_file = home / ".newcode" / "hooks.yaml"
    project_file = workspace / ".newcode" / "hooks.yaml"
    user_file.write_text(
        "network:\n  enabled: true\n  allow_hosts: [example.com]\nhooks: []\n",
        encoding="utf-8",
    )
    project_file.write_text(
        "hooks:\n"
        "  - {id: fetch, event: system_start, action: {type: http_request, url: 'https://example.com/fake'}}\n",
        encoding="utf-8",
    )

    class SocketFixture:
        def __init__(self):
            response_body = b"http-secret-response"
            response_header = f"HTTP/1.1 200 OK\r\nContent-Length: {len(response_body)}\r\n\r\n".encode()
            self.stream = BytesIO(response_header + response_body)
            self.sent = bytearray()

        def getpeername(self):
            return "93.184.216.34", 443

        def sendall(self, data):
            self.sent.extend(data)

        def makefile(self, _mode):
            return self.stream

        def close(self):
            self.stream.close()

    class TlsFixture:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED

        def __init__(self):
            self.server_names = []

        def wrap_socket(self, connection, *, server_hostname):
            self.server_names.append(server_hostname)
            return connection

    dials = []
    connections = []
    tls = TlsFixture()

    def connector(address, timeout):
        dials.append((address, timeout))
        connection = SocketFixture()
        connections.append(connection)
        return connection

    monkeypatch.setattr(hook_actions, "_connect_pinned_ip", connector)
    monkeypatch.setattr(hook_actions.ssl, "create_default_context", lambda: tls)
    monkeypatch.setattr(
        hook_actions.socket,
        "getaddrinfo",
        lambda hostname, port, *, type: [
            (2, type, 6, "", ("93.184.216.34", port))
        ] if hostname == "example.com" else [],
    )
    actual_loader = load_hook_rules
    monkeypatch.setattr(cli, "load_hook_rules", lambda root: actual_loader(root, user_home=home))
    archive = SessionArchive(workspace, sessions_root=home / ".newcode" / "sessions")
    cli_output, cli_errors, session = StringIO(), StringIO(), ChatSession()
    result = cli.run_conversation(
        Provider(), session, tool_context=ToolContext(workspace), session_archive=archive,
        input_func=_inputs("/exit"), output=cli_output, error_output=cli_errors,
    )
    assert result == 0
    assert len(dials) == 1
    assert dials[0][0] == ("93.184.216.34", 443)
    assert 0 < dials[0][1] <= 10
    assert tls.server_names == ["example.com"]
    assert connections and b"GET /fake HTTP/1.1" in connections[0].sent
    archive_text = (archive.sessions_root / f"{session.session_id}.jsonl").read_text(encoding="utf-8")
    assert "http-secret-response" not in cli_output.getvalue() + cli_errors.getvalue() + archive_text

    # 未获用户 allowlist 精确授权的 project host 在 loader 阶段被拒绝，零连接。
    user_file.write_text(
        "network:\n  enabled: true\n  allow_hosts: [example.com]\nhooks: []\n",
        encoding="utf-8",
    )
    project_file.write_text(
        "hooks:\n"
        "  - {id: denied, event: system_start, action: {type: http_request, url: 'https://evil.example/fake'}}\n",
        encoding="utf-8",
    )
    dials.clear()
    result = cli.run_conversation(
        Provider(), ChatSession(), tool_context=ToolContext(workspace),
        input_func=_inputs("/exit"), output=StringIO(), error_output=StringIO(),
    )
    assert result == 0
    assert dials == []

    # host 已授权但 DNS 返回私网地址时，production transport 不会被调用。
    project_file.write_text(
        "hooks:\n"
        "  - {id: private_dns, event: system_start, action: {type: http_request, url: 'https://example.com/fake'}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        hook_actions.socket,
        "getaddrinfo",
        lambda hostname, port, *, type: [
            (2, type, 6, "", ("10.2.3.4", port))
        ] if hostname == "example.com" else [],
    )
    result = cli.run_conversation(
        Provider(), ChatSession(), tool_context=ToolContext(workspace),
        input_func=_inputs("/exit"), output=StringIO(), error_output=StringIO(),
    )
    assert result == 0
    assert dials == []


def test_clear_resume_lifecycle_and_archive_excludes_hook_state(monkeypatch, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    archive = SessionArchive(workspace, sessions_root=tmp_path / "home" / ".newcode" / "sessions")
    previous = ChatSession()
    previous.add_user_message("previous")
    previous_id = archive.create(previous, suffix_factory=lambda: "a1z9")
    archive.checkpoint(previous)
    monkeypatch.setattr(cli, "load_hook_rules", lambda _root: _rules(HookEvent.SESSION_START, HookEvent.SESSION_END))
    observed = []
    original_emit = cli.HookEngine.emit

    def record(self, event, context, **kwargs):
        observed.append((event, context.fields.get("session.id"), self.generation))
        return original_emit(self, event, context, **kwargs)

    monkeypatch.setattr(cli.HookEngine, "emit", record)
    original_checkpoint = archive.checkpoint

    def checkpoint(session, **kwargs):
        observed.append(("checkpoint", session.session_id, None))
        return original_checkpoint(session, **kwargs)

    monkeypatch.setattr(archive, "checkpoint", checkpoint)
    provider = Provider()
    current = ChatSession()
    cli.run_conversation(
        provider, current, session_archive=archive, tool_context=ToolContext(workspace),
        input_func=_inputs("/clear", f"/resume {previous_id}", "hello", "/exit"),
        output=StringIO(),
    )
    events = [item[0] for item in observed if item[0] in (HookEvent.SESSION_START, HookEvent.SESSION_END)]
    assert events == [
        HookEvent.SESSION_START, HookEvent.SESSION_END,
        HookEvent.SESSION_START, HookEvent.SESSION_END,
        HookEvent.SESSION_START, HookEvent.SESSION_END,
    ]
    assert observed[0][2] == 0
    assert observed[-1][2] == 2
    end_and_checkpoint = [item[0] for item in observed if item[0] in (HookEvent.SESSION_END, "checkpoint")]
    assert end_and_checkpoint[:4] == [
        HookEvent.SESSION_END, "checkpoint",
        HookEvent.SESSION_END, "checkpoint",
    ]
    assert end_and_checkpoint[-2:] == [HookEvent.SESSION_END, "checkpoint"]
    assert str(provider.messages[0]).count("MARKER_0") == 1
    restored = archive.restore(previous_id)
    assert restored.session is not None
    assert "MARKER_0" not in str(restored.session.messages)
    assert "MARKER_0" not in (archive.sessions_root / f"{previous_id}.jsonl").read_text(encoding="utf-8")


def test_turn_once_scope_advances_across_cli_turns(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cli, "load_hook_rules", lambda _root: _rules(HookEvent.TURN_START))
    provider = Provider()
    cli.run_conversation(
        provider, ChatSession(), tool_context=ToolContext(tmp_path),
        input_func=_inputs("first", "second", "/exit"), output=StringIO(),
    )
    assert len(provider.messages) == 2
    assert all("MARKER_0" in str(messages) for messages in provider.messages)


def test_cli_shell_gateway_visibility_tracks_current_mode(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cli, "load_hook_rules", lambda _root: _rules(HookEvent.SESSION_START))
    gateways = []
    original_gateway = cli.HookToolGateway

    def capture_gateway(*args, **kwargs):
        gateway = original_gateway(*args, **kwargs)
        gateways.append(gateway)
        return gateway

    monkeypatch.setattr(cli, "HookToolGateway", capture_gateway)
    values = iter(("/plan", "/do", "/exit"))
    checked = []

    def read(_prompt):
        if gateways:
            checked.append(gateways[0].tool_visible("run_command"))
        return next(values)

    cli.run_conversation(
        Provider(), ChatSession(), tool_context=ToolContext(tmp_path),
        input_func=read, output=StringIO(),
    )
    assert checked == [True, False, True]


@pytest.mark.parametrize("exit_value", ["/exit", EOFError(), KeyboardInterrupt()])
def test_hook_shutdown_on_normal_exit_paths(monkeypatch, tmp_path: Path, exit_value):
    monkeypatch.setattr(cli, "load_hook_rules", lambda _root: _rules(HookEvent.SESSION_END))
    shutdowns = []
    original = cli.HookActionRunner.shutdown

    def shutdown(self, timeout_seconds=1.0):
        shutdowns.append(timeout_seconds)
        return original(self, timeout_seconds)

    monkeypatch.setattr(cli.HookActionRunner, "shutdown", shutdown)
    assert cli.run_conversation(
        Provider(), ChatSession(), tool_context=ToolContext(tmp_path),
        input_func=_inputs(exit_value), output=StringIO(),
    ) == 0
    assert shutdowns == [1.0]


def test_exception_and_hook_cleanup_failure_do_not_skip_memory_context(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(cli, "load_hook_rules", lambda _root: _rules(HookEvent.SESSION_END))
    order = []

    class Memory:
        def shutdown(self):
            order.append("memory")

    class Context:
        def cleanup(self):
            order.append("context")

    def failing_shutdown(self, timeout_seconds=1.0):
        order.append("hook")
        raise RuntimeError("secret-token")

    monkeypatch.setattr(cli.HookActionRunner, "shutdown", failing_shutdown)
    monkeypatch.setattr(cli, "_consume_agent_events", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("agent failed")))
    with pytest.raises(RuntimeError, match="agent failed"):
        cli.run_conversation(
            Provider(), ChatSession(), tool_context=ToolContext(tmp_path),
            context_manager=Context(), memory_service=Memory(),
            input_func=_inputs("hello"), output=StringIO(),
        )
    assert order == ["hook", "memory", "context"]


def test_full_main_cleanup_order_and_single_memory_owner(monkeypatch, tmp_path: Path):
    order = []
    config = SimpleNamespace(
        api_key_env="NONE", workspace_root=str(tmp_path), tool_timeout_seconds=1,
        command_timeout_seconds=1, permission_mode=SimpleNamespace(value="default"),
    )
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "resolve_api_key", lambda _name: "secret-token")
    monkeypatch.setattr(cli, "DeepSeekProvider", lambda **_kwargs: Provider())
    monkeypatch.setattr(cli, "load_permission_rules", lambda _root: None)
    monkeypatch.setattr(cli, "load_mcp_config", lambda _root: MCPConfigLoadResult())
    monkeypatch.setattr(cli, "load_hook_rules", lambda _root: _rules(HookEvent.SESSION_END, HookEvent.SYSTEM_END))

    class Runtime:
        def shutdown(self):
            order.append("mcp_runtime")

    class Manager:
        def __init__(self, _servers, *, runtime):
            self.runtime = runtime

        def discover_all(self):
            return {}

        def shutdown(self):
            order.append("mcp_manager")
            raise RuntimeError("cleanup failed")

    class Archive:
        def __init__(self, *_args, **_kwargs):
            pass

        def cleanup_stale(self, **_kwargs):
            pass

        def create(self, session):
            session.session_id = "20260924-120000-a1z9"

        def checkpoint(self, _session):
            order.append("checkpoint")
            raise RuntimeError("cleanup failed")

    class Memory:
        def __init__(self, *_args, **_kwargs):
            pass

        def shutdown(self):
            order.append("memory")
            raise RuntimeError("cleanup failed")

    class Context:
        def __init__(self, *_args, **_kwargs):
            pass

        def cleanup(self):
            order.append("context")
            raise RuntimeError("cleanup failed")

    monkeypatch.setattr(cli, "MCPRuntime", Runtime)
    monkeypatch.setattr(cli, "MCPManager", Manager)
    monkeypatch.setattr(cli, "SessionArchive", Archive)
    monkeypatch.setattr(cli, "MemoryService", Memory)
    monkeypatch.setattr(cli, "ContextManager", Context)
    original_run = cli.run_conversation
    monkeypatch.setattr(cli, "run_conversation", lambda **kwargs: original_run(**kwargs, input_func=_inputs("/exit"), output=StringIO()))
    original_emit = cli.HookEngine.emit

    def emit(self, event, context, **kwargs):
        if event in (HookEvent.SESSION_END, HookEvent.SYSTEM_END):
            order.append(event.value)
        return original_emit(self, event, context, **kwargs)

    monkeypatch.setattr(cli.HookEngine, "emit", emit)
    original_shutdown = cli.HookActionRunner.shutdown

    def hook_shutdown(self, timeout_seconds=1.0):
        order.append("hook")
        return original_shutdown(self, timeout_seconds)

    monkeypatch.setattr(cli.HookActionRunner, "shutdown", hook_shutdown)
    assert cli.main([]) == 0
    assert order == [
        "session_end", "checkpoint", "system_end", "hook", "memory",
        "context", "mcp_manager", "mcp_runtime",
    ]


def test_startup_discovery_exception_releases_created_mcp_resources(monkeypatch, tmp_path: Path):
    order = []
    config = SimpleNamespace(
        api_key_env="NONE", workspace_root=str(tmp_path), tool_timeout_seconds=1,
        command_timeout_seconds=1, permission_mode=SimpleNamespace(value="default"),
    )
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "resolve_api_key", lambda _name: "secret-token")
    monkeypatch.setattr(cli, "DeepSeekProvider", lambda **_kwargs: Provider())
    monkeypatch.setattr(cli, "load_permission_rules", lambda _root: None)
    monkeypatch.setattr(cli, "load_mcp_config", lambda _root: MCPConfigLoadResult())

    class Runtime:
        def shutdown(self):
            order.append("runtime")

    class Manager:
        def __init__(self, _servers, *, runtime):
            pass

        def discover_all(self):
            raise RuntimeError("secret-token")

        def shutdown(self):
            order.append("manager")
            raise RuntimeError("cleanup failed")

    monkeypatch.setattr(cli, "MCPRuntime", Runtime)
    monkeypatch.setattr(cli, "MCPManager", Manager)
    errors = StringIO()
    monkeypatch.setattr(cli.sys, "stderr", errors)
    assert cli.main([]) == 1
    assert order == ["manager", "runtime"]
    assert "secret-token" not in errors.getvalue()


def test_main_fallback_closes_memory_once_if_conversation_does_not_take_ownership(monkeypatch, tmp_path: Path):
    order = []
    config = SimpleNamespace(
        api_key_env="NONE", workspace_root=str(tmp_path), tool_timeout_seconds=1,
        command_timeout_seconds=1, permission_mode=SimpleNamespace(value="default"),
    )
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "resolve_api_key", lambda _name: "safe")
    monkeypatch.setattr(cli, "DeepSeekProvider", lambda **_kwargs: Provider())
    monkeypatch.setattr(cli, "load_permission_rules", lambda _root: None)
    monkeypatch.setattr(cli, "load_mcp_config", lambda _root: MCPConfigLoadResult())

    class Runtime:
        def shutdown(self):
            order.append("runtime")

    class Manager:
        def __init__(self, _servers, *, runtime):
            pass

        def discover_all(self):
            return {}

        def shutdown(self):
            order.append("manager")

    class Memory:
        def __init__(self, *_args, **_kwargs):
            pass

        def shutdown(self):
            order.append("memory")

    monkeypatch.setattr(cli, "MCPRuntime", Runtime)
    monkeypatch.setattr(cli, "MCPManager", Manager)
    monkeypatch.setattr(cli, "MemoryService", Memory)
    monkeypatch.setattr(cli, "run_conversation", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("start failed")))
    with pytest.raises(RuntimeError, match="start failed"):
        cli.main([])
    assert order == ["memory", "manager", "runtime"]
