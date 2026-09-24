"""只读、逐条隔离的 Hook YAML 配置加载。"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .conditions import parse_condition
from .types import (
    MAX_ACTION_TEXT_LENGTH, MAX_ACTION_TIMEOUT_SECONDS, RULE_ID_PATTERN,
    HookAction, HookActionType, HookDiagnostic, HookEvent, HookLoadResult,
    HookNetworkPolicy, HookRule, HookSource, HookValidationError,
)


MAX_CONFIG_BYTES = 131_072
MAX_RULES = 256
ALLOWED_HEADERS = frozenset({"accept", "content-type", "user-agent"})
HOST_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")


def load_hook_rules(workspace_root: Path, *, user_home: Path | None = None) -> HookLoadResult:
    """user 未覆盖规则在前，project 有效规则按声明顺序在后。"""

    home = Path.home() if user_home is None else Path(user_home)
    workspace = Path(workspace_root)
    diagnostics: list[HookDiagnostic] = []
    user_data = _read_yaml(home, HookSource.USER, diagnostics)
    project_data = _read_yaml(workspace, HookSource.PROJECT, diagnostics)
    for source, data in ((HookSource.USER, user_data), (HookSource.PROJECT, project_data)):
        if isinstance(data, dict) and set(data) - {"hooks", "network"}:
            diagnostics.append(HookDiagnostic("hook_config_invalid", source))
    network = _parse_network(user_data, diagnostics)
    if isinstance(project_data, dict) and "network" in project_data:
        diagnostics.append(HookDiagnostic("hook_config_invalid", HookSource.PROJECT))
    user_rules = _parse_rules(user_data, HookSource.USER, network, diagnostics)
    project_rules = _parse_rules(project_data, HookSource.PROJECT, network, diagnostics)
    replaced = {rule.id for rule in project_rules}
    return HookLoadResult(
        tuple(rule for rule in user_rules if rule.id not in replaced) + tuple(project_rules),
        network,
        tuple(diagnostics),
    )


def _read_yaml(root: Path, source: HookSource, diagnostics: list[HookDiagnostic]) -> Any:
    try:
        if ".." in root.parts or root.is_symlink() or not root.is_dir():
            raise HookValidationError("hook_config_invalid")
        root = root.resolve(strict=True)
        directory = root / ".newcode"
        path = directory / "hooks.yaml"
        if directory.is_symlink() or path.is_symlink():
            raise HookValidationError("hook_config_invalid")
        if not path.exists():
            if directory.exists() and not directory.is_dir():
                raise HookValidationError("hook_config_invalid")
            return None
        if not path.is_file() or not path.resolve(strict=True).is_relative_to(root):
            raise HookValidationError("hook_config_invalid")
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise HookValidationError("hook_config_invalid")
        with path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
        if data is not None and not isinstance(data, dict):
            raise HookValidationError("hook_config_invalid")
        return data
    except HookValidationError as error:
        diagnostics.append(HookDiagnostic(error.code, source))
    except (OSError, UnicodeError, yaml.YAMLError, ValueError):
        diagnostics.append(HookDiagnostic("hook_config_invalid", source))
    return None


def _parse_network(data: Any, diagnostics: list[HookDiagnostic]) -> HookNetworkPolicy:
    if not isinstance(data, dict) or "network" not in data:
        return HookNetworkPolicy()
    raw = data["network"]
    if not isinstance(raw, dict) or set(raw) - {"enabled", "allow_hosts"}:
        diagnostics.append(HookDiagnostic("hook_config_invalid", HookSource.USER))
        return HookNetworkPolicy()
    enabled = raw.get("enabled", False)
    hosts = raw.get("allow_hosts", [])
    if type(enabled) is not bool or not isinstance(hosts, list) or len(hosts) > 64:
        diagnostics.append(HookDiagnostic("hook_config_invalid", HookSource.USER))
        return HookNetworkPolicy()
    normalized: list[str] = []
    for host in hosts:
        if not isinstance(host, str) or not _valid_host(host):
            diagnostics.append(HookDiagnostic("hook_config_invalid", HookSource.USER))
            return HookNetworkPolicy()
        canonical = host.lower()
        if canonical not in normalized:
            normalized.append(canonical)
    return HookNetworkPolicy(enabled, tuple(normalized))


def _valid_host(host: str) -> bool:
    if not host.isascii() or len(host) > 253 or HOST_PATTERN.fullmatch(host.lower()) is None:
        return False
    if ".." in host or host.startswith(".") or host.endswith(".") or host.lower() == "localhost":
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return all(0 < len(label) <= 63 and not label.startswith("-") and not label.endswith("-") for label in host.split("."))
    return address.is_global


def _parse_rules(
    data: Any, source: HookSource, network: HookNetworkPolicy,
    diagnostics: list[HookDiagnostic],
) -> list[HookRule]:
    if data is None:
        return []
    raw_rules = data.get("hooks", [])
    if not isinstance(raw_rules, list) or len(raw_rules) > MAX_RULES:
        diagnostics.append(HookDiagnostic("hook_config_invalid", source))
        return []
    rules: list[HookRule] = []
    seen: set[str] = set()
    for raw in raw_rules:
        safe_id = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(safe_id, str) or RULE_ID_PATTERN.fullmatch(safe_id) is None:
            safe_id = None
        try:
            rule = _parse_rule(raw, source, network)
            if rule.id in seen:
                raise HookValidationError("hook_rule_invalid")
        except HookValidationError as error:
            diagnostics.append(HookDiagnostic(error.code, source, safe_id))
            continue
        seen.add(rule.id)
        rules.append(rule)
    return rules


def _parse_rule(raw: Any, source: HookSource, network: HookNetworkPolicy) -> HookRule:
    if not isinstance(raw, dict) or set(raw) - {"id", "event", "action", "if", "once", "async", "deny", "reason"}:
        raise HookValidationError("hook_rule_invalid")
    name = raw.get("id")
    if not isinstance(name, str) or RULE_ID_PATTERN.fullmatch(name) is None:
        raise HookValidationError("hook_rule_invalid")
    try:
        event = HookEvent(raw.get("event"))
    except (ValueError, TypeError):
        raise HookValidationError("hook_event_invalid") from None
    condition = parse_condition(raw["if"]) if "if" in raw else None
    action = _parse_action(raw.get("action"), network)
    return HookRule(
        id=name, event=event, action=action, source=source, condition=condition,
        once=raw.get("once", False), async_requested=raw.get("async", False),
        deny=raw.get("deny", False), reason=raw.get("reason"),
    )


def _parse_action(raw: Any, network: HookNetworkPolicy) -> HookAction:
    if not isinstance(raw, dict):
        raise HookValidationError("hook_action_invalid")
    try:
        kind = HookActionType(raw.get("type"))
    except (ValueError, TypeError):
        raise HookValidationError("hook_action_invalid") from None
    allowed: dict[HookActionType, set[str]] = {
        HookActionType.SHELL: {"type", "command", "timeout_seconds"},
        HookActionType.PROMPT_INJECTION: {"type", "text"},
        HookActionType.HTTP_REQUEST: {"type", "url", "method", "headers", "body", "timeout_seconds"},
        HookActionType.SUBAGENT: {"type"},
    }
    if set(raw) - allowed[kind]:
        raise HookValidationError("hook_action_invalid")
    args = {key: value for key, value in raw.items() if key != "type"}
    if kind is HookActionType.SHELL:
        command = args.get("command")
        if not _bounded_text(command) or any(mark in command for mark in ("$", "%", "{", "}")):
            raise HookValidationError("hook_action_invalid")
        _validate_timeout(args.get("timeout_seconds", 10))
    elif kind is HookActionType.PROMPT_INJECTION:
        if not _bounded_text(args.get("text")):
            raise HookValidationError("hook_action_invalid")
    elif kind is HookActionType.HTTP_REQUEST:
        _validate_http(args, network)
    return HookAction(kind, args)


def _bounded_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_ACTION_TEXT_LENGTH


def _validate_timeout(value: Any) -> None:
    if type(value) not in (int, float) or not 0 < value <= MAX_ACTION_TIMEOUT_SECONDS:
        raise HookValidationError("hook_action_invalid")


def _validate_http(args: dict[str, Any], network: HookNetworkPolicy) -> None:
    url = args.get("url")
    if not isinstance(url, str) or len(url) > 2048 or any(ord(char) < 33 or char.isspace() for char in url):
        raise HookValidationError("hook_action_invalid")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise HookValidationError("hook_action_invalid") from None
    if (
        parts.scheme != "https" or not hostname or not _valid_host(hostname)
        or parts.username is not None or parts.password is not None
        or parts.fragment or port not in (None, 443)
    ):
        raise HookValidationError("hook_action_invalid")
    if not network.enabled:
        raise HookValidationError("hook_http_disabled")
    if hostname.lower() not in network.allow_hosts:
        raise HookValidationError("hook_http_denied")
    if args.get("method", "GET") not in ("GET", "POST"):
        raise HookValidationError("hook_action_invalid")
    headers = args.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 3:
        raise HookValidationError("hook_action_invalid")
    for name, value in headers.items():
        if not isinstance(name, str) or name.lower() not in ALLOWED_HEADERS or not isinstance(value, str) or len(value) > 512 or "\n" in value or "\r" in value:
            raise HookValidationError("hook_action_invalid")
    if "body" in args and (not isinstance(args["body"], str) or len(args["body"]) > MAX_ACTION_TEXT_LENGTH):
        raise HookValidationError("hook_action_invalid")
    _validate_timeout(args.get("timeout_seconds", 10))
