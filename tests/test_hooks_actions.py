"""Hook action 仅使用 fake/受控 gateway，并验证后台生命周期。"""

from io import BytesIO
import subprocess
import ssl
import sys
from threading import Event, Lock
from time import monotonic, sleep

import pytest

from newcode.hooks import HookAction, HookActionType, HookContext, HookEngine, HookEvent, HookNetworkPolicy, HookRule, HookSource
from newcode.hooks.actions import (
    BoundedHostResolver, HookActionRunner, HookGatewayResult, HookHttpResponse,
    HookHttpTransportError, HookToolGateway, PinnedHttpsTransport,
)
from newcode.permissions.manager import PermissionManager
from newcode.permissions.rules import PermissionRuleSet
from newcode.permissions.types import (
    PermissionDecisionValue, PermissionLayer, PermissionMatch, PermissionMode,
    PermissionRule, RiskLevel,
)
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolContext, ToolResult, ToolSpec


def test_actions_import_first_does_not_cycle_through_agent_package():
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from newcode.hooks.actions import HookActionRunner; "
            "from newcode.agent.loop import AgentLoop",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _rule(name="r", *, kind=HookActionType.PROMPT_INJECTION, arguments=None, async_requested=False):
    if arguments is None:
        arguments = {"text": "static SOP"}
    return HookRule(name, HookEvent.TURN_START, HookAction(kind, arguments), HookSource.USER,
                    async_requested=async_requested)


def _do_context():
    return HookContext(HookEvent.TURN_START, {"mode": "do"})


def _wait_for(predicate, timeout=1.0):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(0.005)
    return predicate()


def test_prompt_consumed_once_and_reset_discards_old_session():
    runner = HookActionRunner(sensitive_values=("secret-value",))
    rule = _rule(arguments={"text": "static secret-value"})
    context = HookContext(HookEvent.TURN_START)
    assert runner.submit(rule, context, 0)
    next_request = runner.consume_prompt_injections()
    assert len(next_request) == 1 and "[REDACTED]" in next_request[0]
    assert "secret-value" not in next_request[0]
    assert runner.consume_prompt_injections() == ()
    assert runner.submit(rule, context, 0)
    runner.reset_session(1)
    assert runner.consume_prompt_injections() == ()
    assert not runner.submit(rule, context, 0)
    assert runner.shutdown()


def test_prompt_queue_is_bounded_without_persistence():
    runner = HookActionRunner()
    rule = _rule()
    context = HookContext(HookEvent.TURN_START)
    assert all(runner.submit(rule, context, 0) for _ in range(32))
    assert not runner.submit(rule, context, 0)
    assert len(runner.consume_prompt_injections()) == 32
    assert runner.diagnostics[-1].code == "hook_action_failed"
    runner.shutdown()


def test_engine_session_reset_drops_runner_injections():
    runner = HookActionRunner()
    rule = _rule()
    engine = HookEngine((rule,), action_sink=runner.submit, on_session_reset=runner.reset_session)
    engine.emit(HookEvent.TURN_START, HookContext(HookEvent.TURN_START))
    engine.reset_session()
    assert runner.consume_prompt_injections() == ()
    assert engine.generation == 1
    runner.shutdown()


def test_async_injection_cannot_change_consumed_request():
    runner = HookActionRunner()
    rule = _rule(async_requested=True)
    assert runner.submit(rule, HookContext(HookEvent.TURN_START), 0)
    already_built = runner.consume_prompt_injections()
    assert _wait_for(lambda: bool(runner.consume_prompt_injections()) or bool(already_built))
    # 无论完成在 fence 前后，单个 injection 只可被一次消费。
    assert runner.consume_prompt_injections() == ()
    assert runner.shutdown()


def test_async_injection_after_request_fence_goes_to_next_request():
    shell = _BlockingShell()
    runner = HookActionRunner(shell_gateway=shell)
    context = _do_context()
    assert runner.submit(_rule(kind=HookActionType.SHELL, arguments={"command": "git status"},
                               async_requested=True), context, 0)
    assert shell.entered.wait(1)
    assert runner.submit(_rule(async_requested=True), context, 0)
    sent_messages = runner.consume_prompt_injections()
    assert sent_messages == ()
    shell.release.set()
    observed = []

    def next_request_has_background():
        observed.extend(runner.consume_prompt_injections())
        return bool(observed)

    assert _wait_for(next_request_has_background)
    assert len(observed) == 1
    assert sent_messages == ()
    assert runner.consume_prompt_injections() == ()
    runner.shutdown()


class _BlockingShell:
    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.calls = 0

    def run_shell(self, command, timeout_seconds):
        self.calls += 1
        self.entered.set()
        self.release.wait()
        return HookGatewayResult(True)


def test_single_worker_queue_full_shutdown_bounded_and_idempotent():
    shell = _BlockingShell()
    runner = HookActionRunner(shell_gateway=shell, queue_capacity=1)
    rule = _rule(kind=HookActionType.SHELL, arguments={"command": "git status"}, async_requested=True)
    context = _do_context()
    assert runner.submit(rule, context, 0)
    assert shell.entered.wait(1)
    assert runner.submit(rule, context, 0)
    assert not runner.submit(rule, context, 0)
    started = monotonic()
    assert not runner.shutdown(0.02)
    assert monotonic() - started < 0.5
    assert "hook_shutdown_timeout" in {item.code for item in runner.diagnostics}
    shell.release.set()
    assert _wait_for(lambda: runner.shutdown(0.02))
    assert shell.calls == 1
    assert not runner.submit(rule, context, 0)


def test_async_shell_actions_keep_submission_order():
    calls = []
    lock = Lock()

    class OrderedShell:
        def run_shell(self, command, timeout_seconds):
            with lock:
                calls.append(command)
            return HookGatewayResult(True)

    runner = HookActionRunner(shell_gateway=OrderedShell())
    context = _do_context()
    for name in ("git status", "git diff", "git log"):
        assert runner.submit(_rule(name.replace(" ", "_"), kind=HookActionType.SHELL,
                                   arguments={"command": name}, async_requested=True), context, 0)
    assert _wait_for(lambda: len(calls) == 3)
    assert calls == ["git status", "git diff", "git log"]
    runner.shutdown()


class _FakeHttp:
    def __init__(self, *, peer_ip="93.184.216.34", status=200):
        self.calls = []
        self.peer_ip = peer_ip
        self.status = status

    def request_pinned(self, **kwargs):
        self.calls.append(kwargs)
        return HookHttpResponse(self.status, self.peer_ip, b"secret response")


class _TlsFixture:
    check_hostname = True
    verify_mode = ssl.CERT_REQUIRED

    def __init__(self):
        self.server_names = []

    def wrap_socket(self, connection, *, server_hostname):
        self.server_names.append(server_hostname)
        return connection


class _SocketFixture:
    def __init__(self, peer_ip="93.184.216.34", response=None):
        self.peer_ip = peer_ip
        self.sent = bytearray()
        self.stream = BytesIO(response or b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nfake")

    def getpeername(self):
        return self.peer_ip, 443

    def sendall(self, data):
        self.sent.extend(data)

    def makefile(self, _mode):
        return self.stream

    def close(self):
        self.stream.close()


def test_production_transport_pins_connector_and_tls_hostname():
    connection = _SocketFixture()
    tls = _TlsFixture()
    dialed = []
    transport = PinnedHttpsTransport(
        connector=lambda address, timeout: (dialed.append((address, timeout)) or connection),
        ssl_context=tls,
    )
    response = transport.request_pinned(
        url="https://example.com/a?q=1", method="GET", headers={"Accept": "application/json"},
        body=None, timeout_seconds=2, resolved_ip="93.184.216.34",
        allow_redirects=False, trust_env=False,
    )
    assert dialed[0][0] == ("93.184.216.34", 443)
    assert tls.server_names == ["example.com"]
    assert response.status == 200 and response.peer_ip == "93.184.216.34"
    assert response.body == b"fake"
    assert b"GET /a?q=1 HTTP/1.1" in connection.sent
    assert b"Host: example.com" in connection.sent


def test_production_transport_rejects_peer_mismatch_before_http_request():
    connection = _SocketFixture(peer_ip="93.184.216.35")
    tls = _TlsFixture()
    transport = PinnedHttpsTransport(
        connector=lambda _address, _timeout: connection,
        ssl_context=tls,
    )
    with pytest.raises(HookHttpTransportError):
        transport.request_pinned(
            url="https://example.com/", method="GET", headers={}, body=None,
            timeout_seconds=1, resolved_ip="93.184.216.34",
            allow_redirects=False, trust_env=False,
        )
    assert not connection.sent
    assert not tls.server_names


def test_production_transport_total_timeout_is_bounded():
    release = Event()

    class SlowStream(BytesIO):
        def readline(self, *args, **kwargs):
            release.wait()
            return super().readline(*args, **kwargs)

    class SlowSocket(_SocketFixture):
        def __init__(self):
            super().__init__()
            self.stream = SlowStream(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

    transport = PinnedHttpsTransport(
        connector=lambda _address, _timeout: SlowSocket(), ssl_context=_TlsFixture(),
    )
    started = monotonic()
    try:
        with pytest.raises(TimeoutError):
            transport.request_pinned(
                url="https://example.com/", method="GET", headers={}, body=None,
                timeout_seconds=0.02, resolved_ip="93.184.216.34",
                allow_redirects=False, trust_env=False,
            )
        assert monotonic() - started < 0.5
    finally:
        release.set()


def test_production_transport_requires_verified_tls_context():
    class UnverifiedTls(_TlsFixture):
        verify_mode = ssl.CERT_NONE

    with pytest.raises(ValueError):
        PinnedHttpsTransport(connector=lambda *_args: None, ssl_context=UnverifiedTls())


@pytest.mark.parametrize("overrides", [
    {"url": "https://user:secret@example.com/"},
    {"url": "https://example.com/", "headers": {"Authorization": "secret"}},
    {"resolved_ip": "10.0.0.1"},
    {"allow_redirects": True},
    {"trust_env": True},
])
def test_production_transport_rejects_unsafe_target_or_options_without_connect(overrides):
    calls = []
    transport = PinnedHttpsTransport(
        connector=lambda address, timeout: (calls.append((address, timeout)) or _SocketFixture()),
        ssl_context=_TlsFixture(),
    )
    args = {
        "url": "https://example.com/", "method": "GET", "headers": {}, "body": None,
        "timeout_seconds": 2, "resolved_ip": "93.184.216.34",
        "allow_redirects": False, "trust_env": False,
    }
    args.update(overrides)
    with pytest.raises(HookHttpTransportError):
        transport.request_pinned(**args)
    assert calls == []


def test_bounded_host_resolver_returns_only_dns_addresses(monkeypatch):
    monkeypatch.setattr(
        "newcode.hooks.actions.socket.getaddrinfo",
        lambda host, port, *, type: [
            (2, type, 6, "", ("93.184.216.34", port)),
            (2, type, 6, "", ("93.184.216.34", port)),
        ] if host == "example.com" else [],
    )
    assert BoundedHostResolver(0.5)("example.com") == ("93.184.216.34",)


def test_side_effect_actions_require_explicit_do_mode_sync_and_async():
    class CountingShell:
        def __init__(self):
            self.calls = 0

        def run_shell(self, command, timeout_seconds):
            self.calls += 1
            return HookGatewayResult(True)

    shell = CountingShell()
    transport = _FakeHttp()
    runner = HookActionRunner(
        network=HookNetworkPolicy(True, ("example.com",)),
        shell_gateway=shell, http_transport=transport,
        resolver=lambda _host: ("93.184.216.34",),
    )
    for context in (HookContext(HookEvent.TURN_START, {"mode": "plan"}), HookContext(HookEvent.TURN_START)):
        assert not runner.submit(_rule(kind=HookActionType.SHELL, arguments={"command": "git status"}), context, 0)
        assert not runner.submit(_http_rule(), context, 0)
        assert runner.submit(_rule(kind=HookActionType.SHELL, arguments={"command": "git status"}, async_requested=True), context, 0)
    assert _wait_for(lambda: len(runner.diagnostics) == 6)
    assert shell.calls == 0
    assert transport.calls == []
    runner.shutdown()


def _http_rule(url="https://example.com/a", headers=None):
    return _rule(kind=HookActionType.HTTP_REQUEST, arguments={
        "url": url, "headers": headers or {}, "timeout_seconds": 1,
    })


def test_http_default_disabled_and_public_fake_pinned_success():
    transport = _FakeHttp()
    context = _do_context()
    disabled = HookActionRunner(http_transport=transport, resolver=lambda _host: ("93.184.216.34",))
    assert not disabled.submit(_http_rule(), context, 0)
    assert transport.calls == []
    assert disabled.diagnostics[0].code == "hook_http_disabled"
    allowed = HookActionRunner(network=HookNetworkPolicy(True, ("example.com",)),
                               http_transport=transport, resolver=lambda _host: ("93.184.216.34",))
    assert allowed.submit(_http_rule(), context, 0)
    assert len(transport.calls) == 1
    assert transport.calls[0]["resolved_ip"] == "93.184.216.34"
    assert transport.calls[0]["allow_redirects"] is False
    assert transport.calls[0]["trust_env"] is False
    assert "secret response" not in str(allowed.diagnostics)
    disabled.shutdown()
    allowed.shutdown()


@pytest.mark.parametrize(("url", "addresses", "headers"), [
    ("https://other.example/", ("93.184.216.34",), {}),
    ("https://example.com/", ("127.0.0.1",), {}),
    ("https://example.com/", ("10.1.2.3",), {}),
    ("https://example.com/", ("169.254.1.1",), {}),
    ("https://example.com/", ("93.184.216.34", "192.168.1.2"), {}),
    ("https://127.0.0.1/", ("93.184.216.34",), {}),
    ("https://localhost/", ("93.184.216.34",), {}),
    ("https://user:password@example.com/", ("93.184.216.34",), {}),
    ("https://example.com/", ("93.184.216.34",), {"Authorization": "secret"}),
    ("https://example.com/", ("93.184.216.34",), {"Cookie": "secret"}),
    ("https://example.com/", ("93.184.216.34",), {"Proxy-Authorization": "secret"}),
])
def test_http_ssrf_and_header_rejections_have_zero_requests(url, addresses, headers):
    transport = _FakeHttp()
    policy = HookNetworkPolicy(True, ("example.com", "localhost", "127.0.0.1"))
    runner = HookActionRunner(network=policy, http_transport=transport, resolver=lambda _host: addresses)
    assert not runner.submit(_http_rule(url, headers), _do_context(), 0)
    assert not transport.calls
    assert "password" not in str(runner.diagnostics)
    runner.shutdown()


def test_http_redirect_peer_mismatch_and_timeout_only_diagnostics():
    context = _do_context()
    for peer_ip, status in (("93.184.216.35", 200), ("93.184.216.34", 302)):
        transport = _FakeHttp(peer_ip=peer_ip, status=status)
        runner = HookActionRunner(network=HookNetworkPolicy(True, ("example.com",)),
                                  http_transport=transport, resolver=lambda _host: ("93.184.216.34",))
        assert not runner.submit(_http_rule(), context, 0)
        assert runner.diagnostics[0].code == "hook_http_denied"
        runner.shutdown()

    class TimeoutTransport:
        def request_pinned(self, **_kwargs):
            raise TimeoutError("secret response")

    runner = HookActionRunner(network=HookNetworkPolicy(True, ("example.com",)),
                              http_transport=TimeoutTransport(), resolver=lambda _host: ("93.184.216.34",))
    assert not runner.submit(_http_rule(), context, 0)
    assert runner.diagnostics[0].code == "hook_timeout"
    assert "secret" not in str(runner.diagnostics)
    runner.shutdown()


def test_http_slow_resolver_times_out_before_transport():
    transport = _FakeHttp()

    def slow_resolver(_host):
        sleep(0.02)
        return ("93.184.216.34",)

    runner = HookActionRunner(network=HookNetworkPolicy(True, ("example.com",)),
                              http_transport=transport, resolver=slow_resolver)
    rule = _http_rule()
    rule = HookRule(rule.id, rule.event, HookAction(rule.action.type, {
        **rule.action.arguments, "timeout_seconds": 0.001,
    }), rule.source)
    assert not runner.submit(rule, _do_context(), 0)
    assert transport.calls == []
    assert runner.diagnostics[0].code == "hook_timeout"
    runner.shutdown()


def test_subagent_placeholder_has_no_creation_or_model_result():
    runner = HookActionRunner()
    rule = _rule(kind=HookActionType.SUBAGENT, arguments={})
    assert not runner.submit(rule, HookContext(HookEvent.TURN_START), 0)
    assert runner.diagnostics[0].code == "hook_subagent_not_available"
    assert runner.consume_prompt_injections() == ()
    runner.shutdown()


class _FakeRunCommand:
    def __init__(self):
        self.calls = 0
        self.spec = ToolSpec("run_command", "fake", {"type": "object"})

    def run(self, arguments, context):
        self.calls += 1
        return ToolResult.success("run_command", {"stdout": "secret output", "command": arguments["command"]})


class _CountingConfirmer:
    def __init__(self):
        self.calls = 0

    def confirm(self, request, decision):
        self.calls += 1
        raise AssertionError("Hook 不得触发 CLI/HITL")


def _gateway(tmp_path, mode):
    tool = _FakeRunCommand()
    registry = ToolRegistry()
    registry.register(tool)
    confirmer = _CountingConfirmer()
    gateway = HookToolGateway(PermissionManager(mode=mode, confirmer=confirmer), registry,
                              ToolContext(workspace_root=tmp_path))
    return gateway, tool, confirmer


def test_shell_confirmation_sync_and_async_fail_closed(tmp_path):
    gateway, tool, confirmer = _gateway(tmp_path, PermissionMode.STRICT)
    assert not gateway.run_shell("git status", 1).ok
    runner = HookActionRunner(shell_gateway=gateway)
    sync_rule = _rule(kind=HookActionType.SHELL, arguments={"command": "git status", "timeout_seconds": 1})
    assert not runner.submit(sync_rule, _do_context(), 0)
    rule = _rule(kind=HookActionType.SHELL, arguments={"command": "git status", "timeout_seconds": 1},
                 async_requested=True)
    assert runner.submit(rule, _do_context(), 0)
    assert _wait_for(lambda: len(runner.diagnostics) == 2)
    assert tool.calls == confirmer.calls == 0
    assert all(item.code == "hook_action_failed" for item in runner.diagnostics)
    runner.shutdown()


def test_shell_allow_uses_scheduler_executor_but_keeps_toolresult_internal(tmp_path):
    gateway, tool, confirmer = _gateway(tmp_path, PermissionMode.DEFAULT)
    runner = HookActionRunner(shell_gateway=gateway)
    rule = _rule(kind=HookActionType.SHELL, arguments={"command": "git status", "timeout_seconds": 1})
    assert runner.submit(rule, _do_context(), 0)
    assert tool.calls == 1 and confirmer.calls == 0
    assert not runner.diagnostics
    assert runner.consume_prompt_injections() == ()
    assert "secret output" not in str(runner.diagnostics)
    runner.shutdown()


def test_shell_gateway_rejects_tool_hidden_by_active_skill(tmp_path):
    tool = _FakeRunCommand()
    registry = ToolRegistry()
    registry.register(tool)
    gateway = HookToolGateway(
        PermissionManager(mode=PermissionMode.TRUSTED), registry,
        ToolContext(workspace_root=tmp_path),
        tool_visible=lambda name: name == "read_file",
    )
    assert not gateway.run_shell("git status", 1).ok
    assert tool.calls == 0


def test_shell_hard_deny_even_in_trusted_mode(tmp_path):
    gateway, tool, confirmer = _gateway(tmp_path, PermissionMode.TRUSTED)
    assert not gateway.run_shell("shutdown /s", 1).ok
    assert tool.calls == confirmer.calls == 0


def test_shell_chaining_is_conservatively_rejected(tmp_path):
    gateway, tool, confirmer = _gateway(tmp_path, PermissionMode.TRUSTED)
    assert not gateway.run_shell("git status; echo unsafe", 1).ok
    assert tool.calls == confirmer.calls == 0


def test_shell_explicit_permission_deny_precedes_scheduler(tmp_path):
    tool = _FakeRunCommand()
    registry = ToolRegistry()
    registry.register(tool)
    rule = PermissionRule(
        id="deny_git", tool="run_command", match=PermissionMatch(command="git status"),
        action=PermissionDecisionValue.DENY, reason="Denied", risk_level=RiskLevel.LOW,
        source=PermissionLayer.USER_GLOBAL_RULES,
    )
    manager = PermissionManager(
        mode=PermissionMode.TRUSTED,
        user_global_rules=PermissionRuleSet(PermissionLayer.USER_GLOBAL_RULES, (rule,)),
    )
    gateway = HookToolGateway(manager, registry, ToolContext(workspace_root=tmp_path))
    assert not gateway.run_shell("git status", 1).ok
    assert tool.calls == 0
