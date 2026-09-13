from __future__ import annotations

from types import SimpleNamespace

import pytest

from newcode.agent.mode import AgentMode, allowed_tool_names
from newcode.mcp.config import MCPConfigLoadResult
from newcode.mcp.types import MCPServerConfig, MCPServerError, MCPToolDescriptor


class Runtime:
    def __init__(self): self.shutdown_calls = 0
    def shutdown(self): self.shutdown_calls += 1


class Manager:
    instances = []
    def __init__(self, servers, *, runtime): self.servers, self.runtime, self.shutdown_calls = list(servers), runtime, 0; Manager.instances.append(self)
    def discover_all(self): return {"good": (MCPToolDescriptor("good", "search", "Search", {"type": "object"}),)} if self.servers else {}
    def shutdown(self): self.shutdown_calls += 1


def patch_main(monkeypatch, tmp_path, mcp_result, captured):
    import newcode.cli as cli
    config = SimpleNamespace(api_key_env="NO_KEY", workspace_root=str(tmp_path), tool_timeout_seconds=1, command_timeout_seconds=1, permission_mode=SimpleNamespace(value="default"))
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "resolve_api_key", lambda _: "api-secret")
    monkeypatch.setattr(cli, "DeepSeekProvider", lambda **_: object())
    monkeypatch.setattr(cli, "load_permission_rules", lambda _: None)
    monkeypatch.setattr(cli, "load_mcp_config", lambda _: mcp_result)
    monkeypatch.setattr(cli, "MCPRuntime", Runtime)
    monkeypatch.setattr(cli, "MCPManager", Manager)
    def run(**kwargs): captured.update(kwargs); return 0
    monkeypatch.setattr(cli, "run_conversation", run)
    return cli


def test_cli_without_mcp_config_keeps_builtin_registry(monkeypatch, tmp_path):
    captured = {}; cli = patch_main(monkeypatch, tmp_path, MCPConfigLoadResult(), captured)
    assert cli.main([]) == 0
    assert len(captured["registry"].names()) == 6
    assert Manager.instances[-1].shutdown_calls == 1
    assert Manager.instances[-1].runtime.shutdown_calls == 1


def test_cli_registers_discovered_adapter_and_plan_hides_it(monkeypatch, tmp_path):
    captured = {}; result = MCPConfigLoadResult(servers={"good": MCPServerConfig("good", "stdio", command="fake")})
    cli = patch_main(monkeypatch, tmp_path, result, captured)
    assert cli.main([]) == 0
    names = captured["registry"].names(); mcp_name = next(name for name in names if name.startswith("mcp__"))
    assert mcp_name in allowed_tool_names(AgentMode.DO, captured["registry"])
    assert mcp_name not in allowed_tool_names(AgentMode.PLAN, captured["registry"])


def test_cli_warning_is_secret_safe_and_finally_runs_on_conversation_exception(monkeypatch, tmp_path, capsys):
    captured = {}; result = MCPConfigLoadResult(errors={"bad": MCPServerError("mcp_config_error", "secret-token")})
    cli = patch_main(monkeypatch, tmp_path, result, captured)
    monkeypatch.setattr(cli, "run_conversation", lambda **_: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError): cli.main([])
    assert "secret-token" not in capsys.readouterr().err
    assert Manager.instances[-1].shutdown_calls == 1
    assert Manager.instances[-1].runtime.shutdown_calls == 1


@pytest.mark.parametrize("exit_path", ["eof", "exit", "keyboard_interrupt"])
def test_cli_finally_shuts_down_for_normal_conversation_exit_paths(monkeypatch, tmp_path, exit_path):
    captured = {}; cli = patch_main(monkeypatch, tmp_path, MCPConfigLoadResult(), captured)
    monkeypatch.setattr(cli, "run_conversation", lambda **_: 0)
    assert cli.main([]) == 0
    assert Manager.instances[-1].shutdown_calls == 1
    assert Manager.instances[-1].runtime.shutdown_calls == 1
