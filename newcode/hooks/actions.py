"""Hook action 只使用受控工具 gateway 与固定 IP 的 HTTPS transport。"""

from __future__ import annotations

import ipaddress
import http.client
import socket
import ssl
from collections import deque
from dataclasses import dataclass
from threading import Event, Lock, Thread
from time import monotonic
from typing import Callable, Protocol
from urllib.parse import urlsplit

from newcode.context.redaction import redact_text
from newcode.permissions.confirmer import DenyByDefaultConfirmer
from newcode.permissions.manager import PermissionManager
from newcode.permissions.types import PermissionDecisionValue
from newcode.tools.executor import execute_tool_call
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolCall, ToolContext, ToolResult

from .lifecycle import HookWorker
from .types import (
    MAX_ACTION_TEXT_LENGTH, HookActionType, HookContext, HookDiagnostic,
    HookEvent, HookNetworkPolicy, HookRule, HookSource,
)


MAX_HTTP_RESPONSE_BYTES = 65_536
MAX_PENDING_INJECTIONS = 32
ALLOWED_HTTP_HEADERS = frozenset({"accept", "content-type", "user-agent"})
MAX_HTTP_DNS_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class HookGatewayResult:
    ok: bool
    code: str | None = None


class ShellGateway(Protocol):
    def run_shell(self, command: str, timeout_seconds: float) -> HookGatewayResult: ...


@dataclass(frozen=True)
class HookHttpResponse:
    status: int
    peer_ip: str
    body: bytes = b""


class HttpTransport(Protocol):
    def request_pinned(
        self, *, url: str, method: str, headers: dict[str, str],
        body: str | None, timeout_seconds: float, resolved_ip: str,
        allow_redirects: bool, trust_env: bool,
    ) -> HookHttpResponse: ...


class BoundedHostResolver:
    """系统 DNS 查询在有界等待内返回；卡住的系统调用留在 daemon 线程中。"""

    def __init__(self, timeout_seconds: float = MAX_HTTP_DNS_TIMEOUT_SECONDS) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("invalid resolver timeout")
        self.timeout_seconds = float(timeout_seconds)
        self._active = Lock()

    def __call__(self, hostname: str) -> tuple[str, ...]:
        return self.resolve_with_timeout(hostname, self.timeout_seconds)

    def resolve_with_timeout(self, hostname: str, timeout_seconds: float) -> tuple[str, ...]:
        if not hostname or type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise TimeoutError("Hook DNS lookup timed out")
        if not self._active.acquire(blocking=False):
            raise TimeoutError("Hook DNS lookup is busy")
        completed = Event()
        result: dict[str, object] = {}

        def lookup() -> None:
            try:
                records = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
                addresses: list[str] = []
                for record in records:
                    address = record[4][0]
                    if address not in addresses:
                        addresses.append(address)
                        if len(addresses) > 16:
                            break
                result["addresses"] = tuple(addresses)
            except BaseException:
                result["error"] = True
            finally:
                self._active.release()
                completed.set()

        worker = Thread(target=lookup, name="newcode-hook-dns", daemon=True)
        try:
            worker.start()
        except Exception:
            self._active.release()
            raise
        if not completed.wait(float(timeout_seconds)):
            raise TimeoutError("Hook DNS lookup timed out")
        if result.get("error") or not result.get("addresses"):
            raise OSError("Hook DNS lookup failed")
        return result["addresses"]  # type: ignore[return-value]


class HookHttpTransportError(OSError):
    """固定错误类型，不暴露 URL、地址或底层 TLS/socket 异常。"""


def _connect_pinned_ip(address: tuple[str, int], timeout_seconds: float):
    """只把数值 IP 交给 socket.connect，避免连接阶段再次解析 hostname。"""

    host, port = address
    target = ipaddress.ip_address(host)
    family = socket.AF_INET6 if target.version == 6 else socket.AF_INET
    connection = socket.socket(family, socket.SOCK_STREAM)
    connection.settimeout(timeout_seconds)
    try:
        connection.connect((str(target), port))
        return connection
    except Exception:
        connection.close()
        raise


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, port, *, timeout, resolved_ip, connector, context) -> None:
        super().__init__(hostname, port=port, timeout=timeout, context=context)
        self._resolved_ip = str(ipaddress.ip_address(resolved_ip))
        self._connector = connector
        self._tls_context = context
        self.verified_peer_ip: str | None = None

    def connect(self) -> None:
        raw_socket = None
        try:
            raw_socket = self._connector((self._resolved_ip, self.port), self.timeout)
            if _socket_peer_ip(raw_socket) != self._resolved_ip:
                raise HookHttpTransportError("Pinned peer mismatch")
            tls_socket = self._tls_context.wrap_socket(raw_socket, server_hostname=self.host)
            peer_ip = _socket_peer_ip(tls_socket)
            if peer_ip != self._resolved_ip:
                tls_socket.close()
                raise HookHttpTransportError("Pinned peer mismatch")
            self.sock = tls_socket
            self.verified_peer_ip = peer_ip
        except Exception as exc:
            if raw_socket is not None:
                try:
                    raw_socket.close()
                except Exception:
                    pass
            if isinstance(exc, HookHttpTransportError):
                raise
            raise HookHttpTransportError("Pinned HTTPS connection failed") from None


class PinnedHttpsTransport:
    """HTTPS client that connects to a validated IP while checking TLS for the URL host."""

    def __init__(self, *, connector=None, ssl_context: ssl.SSLContext | None = None) -> None:
        self._connector = connector or _connect_pinned_ip
        self._ssl_context = ssl.create_default_context() if ssl_context is None else ssl_context
        self._active_request = Lock()
        if (
            getattr(self._ssl_context, "check_hostname", False) is not True
            or getattr(self._ssl_context, "verify_mode", None) != ssl.CERT_REQUIRED
        ):
            raise ValueError("TLS certificate verification is required")

    def request_pinned(
        self, *, url: str, method: str, headers: dict[str, str],
        body: str | None, timeout_seconds: float, resolved_ip: str,
        allow_redirects: bool, trust_env: bool,
    ) -> HookHttpResponse:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60:
            raise HookHttpTransportError("Invalid HTTPS timeout")
        if not self._active_request.acquire(blocking=False):
            raise HookHttpTransportError("HTTPS transport is busy")
        completed = Event()
        result: dict[str, object] = {}

        def perform() -> None:
            try:
                result["response"] = self._request_pinned(
                    url=url, method=method, headers=headers, body=body,
                    timeout_seconds=float(timeout_seconds), resolved_ip=resolved_ip,
                    allow_redirects=allow_redirects, trust_env=trust_env,
                )
            except TimeoutError:
                result["error"] = "timeout"
            except HookHttpTransportError as exc:
                result["error"] = exc
            except BaseException:
                result["error"] = HookHttpTransportError("HTTPS request failed")
            finally:
                self._active_request.release()
                completed.set()

        worker = Thread(target=perform, name="newcode-hook-http", daemon=True)
        try:
            worker.start()
        except Exception:
            self._active_request.release()
            raise HookHttpTransportError("HTTPS request failed") from None
        if not completed.wait(float(timeout_seconds)):
            raise TimeoutError("Hook HTTPS request timed out")
        error = result.get("error")
        if error == "timeout":
            raise TimeoutError("Hook HTTPS request timed out")
        if isinstance(error, HookHttpTransportError):
            raise error
        response = result.get("response")
        if not isinstance(response, HookHttpResponse):
            raise HookHttpTransportError("HTTPS request failed")
        return response

    def _request_pinned(
        self, *, url: str, method: str, headers: dict[str, str],
        body: str | None, timeout_seconds: float, resolved_ip: str,
        allow_redirects: bool, trust_env: bool,
    ) -> HookHttpResponse:
        if allow_redirects is not False or trust_env is not False:
            raise HookHttpTransportError("Unsafe transport options")
        try:
            parts = urlsplit(url)
            hostname = parts.hostname
            port = parts.port or 443
            target_ip = ipaddress.ip_address(resolved_ip)
        except (TypeError, ValueError):
            raise HookHttpTransportError("Invalid HTTPS target") from None
        if (
            any(ord(char) < 33 or char.isspace() for char in url)
            or parts.scheme != "https" or not hostname or parts.username is not None
            or parts.password is not None or parts.fragment or port != 443
            or not target_ip.is_global or method not in ("GET", "POST")
            or not isinstance(headers, dict) or len(headers) > len(ALLOWED_HTTP_HEADERS)
            or any(
                not isinstance(name, str) or name.lower() not in ALLOWED_HTTP_HEADERS
                or not isinstance(value, str) or len(value) > 512
                or "\r" in value or "\n" in value
                for name, value in headers.items()
            )
            or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60
            or (body is not None and (not isinstance(body, str) or len(body) > MAX_ACTION_TEXT_LENGTH))
        ):
            raise HookHttpTransportError("HTTPS target rejected")

        connection = _PinnedHTTPSConnection(
            hostname, port, timeout=float(timeout_seconds), resolved_ip=str(target_ip),
            connector=self._connector, context=self._ssl_context,
        )
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            connection.request(
                method, path, body=body.encode("utf-8") if body is not None else None,
                headers=headers,
            )
            response = connection.getresponse()
            payload = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
            peer_ip = connection.verified_peer_ip
            if peer_ip is None:
                raise HookHttpTransportError("HTTPS peer unavailable")
            return HookHttpResponse(response.status, peer_ip, payload)
        except TimeoutError:
            raise
        except HookHttpTransportError:
            raise
        except Exception:
            raise HookHttpTransportError("HTTPS request failed") from None
        finally:
            connection.close()


def _socket_peer_ip(connection) -> str:
    try:
        peer = connection.getpeername()
        return str(ipaddress.ip_address(peer[0]))
    except Exception:
        raise HookHttpTransportError("HTTPS peer unavailable") from None


class HookToolGateway:
    """使用已有 Permission→ToolScheduler→executor；内部 ToolResult 不外传。"""

    def __init__(
        self, permission_manager: PermissionManager, registry: ToolRegistry,
        tool_context: ToolContext, *, tool_visible: Callable[[str], bool] | None = None,
    ) -> None:
        # 避免模块加载时触发 agent 包初始化，形成 actions ↔ AgentLoop 循环导入。
        from newcode.agent.scheduler import ToolScheduler

        self.permission_manager = permission_manager
        self.registry = registry
        self.tool_context = tool_context
        self.tool_visible = tool_visible
        self.scheduler = ToolScheduler(registry)

    def run_shell(self, command: str, timeout_seconds: float) -> HookGatewayResult:
        if self.tool_visible is not None:
            try:
                if self.tool_visible("run_command") is not True:
                    return HookGatewayResult(False, "hook_action_failed")
            except Exception:
                return HookGatewayResult(False, "hook_action_failed")
        if (
            not isinstance(command, str) or not command.strip() or len(command) > 4000
            or any(marker in command for marker in ("$", "%", "{", "}", "&", "|", ";", "<", ">", "\n", "\r"))
            or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60
        ):
            return HookGatewayResult(False, "hook_action_failed")
        tool = self.registry.get("run_command")
        if tool is None:
            return HookGatewayResult(False, "hook_action_failed")
        call = ToolCall("hook-internal", "run_command", {
            "command": command, "timeout_seconds": timeout_seconds,
        })
        source = self.permission_manager
        # 共享规则和 mode，但绝不调用 CLI/HITL confirmer，也不写会话授权。
        noninteractive = PermissionManager(
            mode=source.mode,
            session_rules=source.session_rules,
            local_project_rules=source.local_project_rules,
            project_rules=source.project_rules,
            user_global_rules=source.user_global_rules,
            rule_load_errors=source.rule_load_errors,
            confirmer=DenyByDefaultConfirmer(),
        )
        decision = noninteractive.check(call, self.tool_context, tool)
        if decision.decision is not PermissionDecisionValue.ALLOW:
            return HookGatewayResult(False, "hook_action_failed")
        records = self.scheduler.execute(
            [call],
            lambda item: execute_tool_call(item, self.registry, self.tool_context),
            lambda _item: ToolResult.failure("run_command", "unknown_tool", "Unavailable"),
        )
        if len(records) != 1:
            return HookGatewayResult(False, "hook_action_failed")
        result = records[0].result
        if result.ok:
            return HookGatewayResult(True)
        if result.error is not None and result.error.code == "timeout":
            return HookGatewayResult(False, "hook_timeout")
        return HookGatewayResult(False, "hook_action_failed")


class HookActionRunner:
    """结果只到受限诊断；仅 prompt queue 向未来主请求交付静态背景。"""

    def __init__(
        self,
        *,
        network: HookNetworkPolicy | None = None,
        shell_gateway: ShellGateway | None = None,
        resolver: Callable[[str], tuple[str, ...]] | None = None,
        http_transport: HttpTransport | None = None,
        queue_capacity: int = 32,
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        self.network = network or HookNetworkPolicy()
        self.shell_gateway = shell_gateway
        self.resolver = resolver
        self.http_transport = http_transport
        self.sensitive_values = sensitive_values
        self._lock = Lock()
        self._generation = 0
        self._pending: list[str] = []
        self._diagnostics: deque[HookDiagnostic] = deque(maxlen=256)
        self._closed = False
        self._shutdown_reported = False
        self.worker = HookWorker(capacity=queue_capacity, on_error=self._worker_failed)

    @property
    def diagnostics(self) -> tuple[HookDiagnostic, ...]:
        with self._lock:
            return tuple(self._diagnostics)

    def reset_session(self, generation: int) -> None:
        with self._lock:
            self._generation = generation
            self._pending.clear()

    def consume_prompt_injections(self) -> tuple[str, ...]:
        """在最终 messages 构造前调用；取走后无法修改该请求。"""

        with self._lock:
            taken = tuple(self._pending)
            self._pending.clear()
            return taken

    def submit(self, rule: HookRule, context: HookContext, generation: int) -> bool:
        with self._lock:
            if self._closed or generation != self._generation:
                return False
        if rule.async_requested:
            if rule.event is HookEvent.BEFORE_TOOL:
                self._record("hook_action_failed", rule)
                return False
            accepted = self.worker.submit(lambda: self._perform(rule, context, generation))
            if not accepted:
                self._record("hook_action_failed", rule)
            return accepted
        return self._perform(rule, context, generation)

    def shutdown(self, timeout_seconds: float = 1.0) -> bool:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._generation += 1
                self._pending.clear()
        done = self.worker.shutdown(timeout_seconds)
        if not done:
            with self._lock:
                if not self._shutdown_reported:
                    self._diagnostics.append(HookDiagnostic("hook_shutdown_timeout", HookSource.USER))
                    self._shutdown_reported = True
        return done

    def _record(self, code: str, rule: HookRule) -> None:
        with self._lock:
            self._diagnostics.append(HookDiagnostic(code, rule.source, rule.id))

    def _worker_failed(self) -> None:
        with self._lock:
            self._diagnostics.append(HookDiagnostic("hook_action_failed", HookSource.USER))

    def _perform(self, rule: HookRule, context: HookContext, generation: int) -> bool:
        with self._lock:
            if self._closed or generation != self._generation:
                return False
        # Hook 的副作用动作不得借生命周期事件越过主 Agent 的 Plan/Do gate。
        if rule.action.type in (HookActionType.SHELL, HookActionType.HTTP_REQUEST) and context.fields.get("mode") != "do":
            self._record(
                "hook_http_denied" if rule.action.type is HookActionType.HTTP_REQUEST else "hook_action_failed",
                rule,
            )
            return False
        started = monotonic()
        try:
            timeout = float(rule.action.arguments.get("timeout_seconds", 10))
            if not 0 < timeout <= 60:
                raise ValueError("invalid timeout")
            if rule.action.type is HookActionType.PROMPT_INJECTION:
                text = rule.action.arguments["text"]
                if not isinstance(text, str) or not 0 < len(text) <= 4000:
                    raise ValueError("invalid prompt")
                with self._lock:
                    if generation != self._generation or self._closed:
                        return False
                    if len(self._pending) >= MAX_PENDING_INJECTIONS:
                        full = True
                    else:
                        full = False
                        self._pending.append("[Hook 背景；非授权信息，需经工具核验]\n" + redact_text(text, self.sensitive_values))
                if full:
                    self._record("hook_action_failed", rule)
                    return False
                return True
            if rule.action.type is HookActionType.SUBAGENT:
                self._record("hook_subagent_not_available", rule)
                return False
            if rule.action.type is HookActionType.SHELL:
                command = rule.action.arguments.get("command")
                if not isinstance(command, str) or not 0 < len(command) <= 4000 or any(
                    marker in command for marker in ("$", "%", "{", "}", "&", "|", ";", "<", ">", "\n", "\r")
                ):
                    code = "hook_action_failed"
                elif self.shell_gateway is None:
                    code = "hook_action_failed"
                else:
                    result = self.shell_gateway.run_shell(command, timeout)
                    code = result.code if not result.ok else None
            elif rule.action.type is HookActionType.HTTP_REQUEST:
                code = self._http_request(rule, timeout)
            else:
                code = "hook_action_failed"
            if monotonic() - started > timeout:
                code = "hook_timeout"
        except TimeoutError:
            code = "hook_timeout"
        except Exception:
            code = "hook_action_failed"
        with self._lock:
            if self._closed or generation != self._generation:
                return False
        if code is not None:
            self._record(code, rule)
            return False
        return True

    def _http_request(self, rule: HookRule, timeout: float) -> str | None:
        started = monotonic()
        if not self.network.enabled:
            return "hook_http_disabled"
        if self.http_transport is None or self.resolver is None:
            return "hook_http_denied"
        args = rule.action.arguments
        url = args.get("url")
        if not isinstance(url, str) or any(ord(char) < 33 or char.isspace() for char in url):
            return "hook_http_denied"
        try:
            parts = urlsplit(url)
            host = parts.hostname
            port = parts.port
        except ValueError:
            return "hook_http_denied"
        if (
            parts.scheme != "https" or not host or host.lower() not in self.network.allow_hosts
            or host.lower() == "localhost" or host.lower().endswith(".localhost")
            or parts.username is not None or parts.password is not None
            or parts.fragment or port not in (None, 443)
        ):
            return "hook_http_denied"
        headers = args.get("headers", {})
        if not isinstance(headers, dict) or any(
            not isinstance(key, str) or key.lower() not in ALLOWED_HTTP_HEADERS
            or not isinstance(value, str) or len(value) > 512 or "\r" in value or "\n" in value
            for key, value in headers.items()
        ):
            return "hook_http_denied"
        method = args.get("method", "GET")
        if method not in ("GET", "POST"):
            return "hook_http_denied"
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            remaining = timeout - (monotonic() - started)
            if remaining <= 0:
                return "hook_timeout"
            bounded_resolve = getattr(self.resolver, "resolve_with_timeout", None)
            addresses = (
                bounded_resolve(host, remaining)
                if callable(bounded_resolve) else self.resolver(host)
            )
        else:
            addresses = (str(literal),)
        if monotonic() - started >= timeout:
            return "hook_timeout"
        if not addresses or len(addresses) > 16:
            return "hook_http_denied"
        try:
            parsed = tuple(ipaddress.ip_address(item) for item in addresses)
        except ValueError:
            return "hook_http_denied"
        if not all(item.is_global for item in parsed):
            return "hook_http_denied"
        selected = str(parsed[0])
        remaining = timeout - (monotonic() - started)
        if remaining <= 0:
            return "hook_timeout"
        response = self.http_transport.request_pinned(
            url=url, method=method, headers=dict(headers), body=args.get("body"),
            timeout_seconds=remaining, resolved_ip=selected,
            allow_redirects=False, trust_env=False,
        )
        if (
            response.peer_ip != selected or not isinstance(response.body, bytes)
            or len(response.body) > MAX_HTTP_RESPONSE_BYTES
            or not 200 <= response.status < 300
        ):
            return "hook_http_denied" if 300 <= response.status < 400 or response.peer_ip != selected else "hook_action_failed"
        return None
