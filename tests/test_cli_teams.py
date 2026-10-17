"""受控 CLI attachment 与 coordinator 启动验收；没有真实 Provider/网络。"""
from io import StringIO
from types import SimpleNamespace

import pytest

from newcode import cli
from newcode.commands.builtins import CommandRuntime, create_builtin_registry
from newcode.commands.dispatcher import CommandDispatcher
from newcode.commands.ui import FakeUIControl
from newcode.permissions.manager import PermissionManager
from newcode.permissions.types import PermissionMode
from newcode.session import ChatSession
from newcode.teams.config import TeamConfig, TeamConfigResult
from newcode.teams.coordinator import CoordinatorPolicy
from newcode.teams.runtime import TeamAttachment
from newcode.teams.store import TeamStore
from newcode.teams.tools import TEAM_TOOLS
from newcode.tools.types import ToolContext
from test_teams_paths import make_paths
from test_cli_commands import Provider


def setup_cli(tmp_path, monkeypatch, *, coordinator=False):
    paths = make_paths(tmp_path)
    config = TeamConfig(enabled=True, coordinator_enabled=coordinator)
    monkeypatch.setattr(cli, "load_team_config", lambda *a, **kw: TeamConfigResult(config))
    monkeypatch.setattr(cli, "load_hook_rules", lambda *a, **kw: SimpleNamespace(rules=(), diagnostics=()))
    if coordinator:
        monkeypatch.setenv("NEWCODE_TEAM_COORDINATOR", "1")
    else:
        monkeypatch.delenv("NEWCODE_TEAM_COORDINATOR", raising=False)
    runtime = TeamAttachment(store=TeamStore(paths), config=config, coordinator=CoordinatorPolicy(coordinator))
    return paths, runtime


def run_cli(paths, runtime, inputs):
    script = iter(inputs)
    def read(_prompt):
        value = next(script, EOFError())
        if isinstance(value, BaseException):
            raise value
        return value
    provider, output, errors = Provider(), StringIO(), StringIO()
    result = cli.run_conversation(provider, ChatSession(), tool_context=ToolContext(paths.workspace_root),
        input_func=read, output=output, error_output=errors, agent_user_home=paths.home,
        team_attachment=runtime, permission_manager=PermissionManager(mode=PermissionMode.TRUSTED))
    return result, provider, output.getvalue(), errors.getvalue()


def test_team_local_commands_attach_and_detach_do_not_invoke_provider(tmp_path, monkeypatch):
    paths, runtime = setup_cli(tmp_path, monkeypatch)
    result, provider, output, errors = run_cli(paths, runtime, ["/team lead", "/team status", "/team close", "/exit"])
    assert result == 0 and not provider.calls and runtime.role == "ordinary"
    assert '"role": "lead"' in output and '"role": "ordinary"' in output
    assert not tuple(paths.root.glob("team-*"))


def test_team_role_schema_does_not_leak_to_normal_cli_or_after_clear(tmp_path, monkeypatch):
    paths, runtime = setup_cli(tmp_path, monkeypatch)
    _, provider, _, _ = run_cli(paths, runtime, ["普通", "/team lead", "负责人", "/clear", "新会话", "/exit"])
    schemas = [{t["function"]["name"] for t in call[1]} for call in provider.calls]
    assert not schemas[0] & TEAM_TOOLS
    assert TEAM_TOOLS <= schemas[1]
    assert not schemas[2] & TEAM_TOOLS
    assert runtime.generation >= 2 and runtime.role == "ordinary"


@pytest.mark.parametrize("exit_input", ["/exit", EOFError(), KeyboardInterrupt()])
def test_every_exit_detaches_and_keeps_busy_member_ownership(tmp_path, monkeypatch, exit_input):
    paths, runtime = setup_cli(tmp_path, monkeypatch)
    stopped, closed = [], []
    scope = SimpleNamespace(member_id="alice")
    runner = SimpleNamespace(scope=scope, close=lambda: closed.append(True) or False)
    backend = SimpleNamespace(request_stop=lambda s: stopped.append(s), close=lambda **kw: None,
                              join_until=lambda deadline: None)
    runtime._handles["alice"] = (runner, backend)
    result, _, _, _ = run_cli(paths, runtime, [exit_input])
    assert result == 0 and stopped and closed and len(runtime._retired) == 1
    assert runtime.role == "ordinary" and not runtime._handles


def test_resume_detaches_team_and_retains_unstopped_member(tmp_path, monkeypatch):
    from newcode.persistence import SessionArchive
    paths, runtime = setup_cli(tmp_path, monkeypatch)
    archive = SessionArchive(paths.workspace_root, sessions_root=paths.home / "sessions")
    previous = ChatSession()
    previous.add_user_message("之前的会话")
    session_id = archive.create(previous)
    archive.checkpoint(previous)
    runtime.command(("lead",))
    stopped, closed = [], []
    scope = SimpleNamespace(team_id="team-" + "a" * 32, member_id="alice", generation=1)
    runner = SimpleNamespace(scope=scope, close=lambda: closed.append(True) or False)
    backend = SimpleNamespace(request_stop=lambda _scope: stopped.append(True),
        close=lambda **_kwargs: None, join_until=lambda _deadline: None)
    runtime._handles["alice"] = (runner, backend)
    script = iter([f"/resume {session_id}", "继续", "/exit"])
    assert cli.run_conversation(Provider(), ChatSession(), tool_context=ToolContext(paths.workspace_root),
        input_func=lambda _prompt: next(script), output=StringIO(), error_output=StringIO(),
        agent_user_home=paths.home, session_archive=archive, team_attachment=runtime,
        permission_manager=PermissionManager(mode=PermissionMode.TRUSTED)) == 0
    assert runtime.role == "ordinary" and runtime.team_id is None and runtime.generation >= 2
    assert not runtime._handles and any(item[0] is runner for item in runtime._retired)
    assert stopped and closed


def test_startup_and_runtime_exceptions_isolate_team_cleanup(tmp_path, monkeypatch):
    paths, runtime = setup_cli(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError):
        run_cli(paths, runtime, ["/team lead", RuntimeError("fixture-secret")])
    assert runtime.role == "ordinary"


def test_startup_failure_after_team_attachment_detaches_once(tmp_path, monkeypatch):
    paths, runtime = setup_cli(tmp_path, monkeypatch)
    stopped, closed = [], []
    scope = SimpleNamespace(team_id="team-" + "b" * 32, member_id="alice", generation=1)
    runner = SimpleNamespace(scope=scope, close=lambda: closed.append(True) or True)
    backend = SimpleNamespace(request_stop=lambda _scope: stopped.append(True),
        close=lambda **_kwargs: None, join_until=lambda _deadline: None)
    runtime._handles["alice"] = (runner, backend)
    monkeypatch.setattr(cli, "ContextManager", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("startup")))
    with pytest.raises(RuntimeError, match="startup"):
        cli.run_conversation(Provider(), ChatSession(), tool_context=ToolContext(paths.workspace_root),
            input_func=lambda _prompt: "/exit", output=StringIO(), error_output=StringIO(),
            agent_user_home=paths.home, team_attachment=runtime,
            permission_manager=PermissionManager(mode=PermissionMode.TRUSTED))
    assert stopped == [True] and closed == [True]
    assert runtime.role == "ordinary" and not runtime._handles and not runtime._retired


def test_coordinator_startup_does_not_load_hook_actions_and_schema_is_closed(tmp_path, monkeypatch):
    paths, runtime = setup_cli(tmp_path, monkeypatch, coordinator=True)
    monkeypatch.setattr(cli, "load_hook_rules", lambda *a: pytest.fail("不应加载 Hook action"))
    _, provider, _, _ = run_cli(paths, runtime, ["/team lead", "观察", "/exit"])
    names = {t["function"]["name"] for t in provider.calls[0][1]}
    assert names <= {"read_file", "find_files", "search_code"} | TEAM_TOOLS
    assert TEAM_TOOLS <= names


def test_t22_coordinator_requires_both_switches_before_cli_attachment(tmp_path, monkeypatch):
    from newcode.teams.types import TeamError
    paths, runtime = setup_cli(tmp_path, monkeypatch, coordinator=True)
    monkeypatch.delenv("NEWCODE_TEAM_COORDINATOR", raising=False)
    with pytest.raises(TeamError, match="team_coordinator_unavailable"):
        run_cli(paths, runtime, ["/team lead", "集成"])
    assert not tuple(paths.root.glob("team-*"))


def test_static_team_command_contract_and_no_ai_fallback():
    registry = create_builtin_registry(include_team=True)
    assert registry.get("/team") is not None
    dispatcher, ui, calls = CommandDispatcher(registry), FakeUIControl(), []
    runtime = CommandRuntime(registry, lambda: "no_history", lambda: (), lambda s: (), lambda: "", lambda: "",
                             team_command=lambda args: calls.append(args) or "ok")
    for command in ("/team", "/team open", "/team lead forged", "/team delete x"):
        parsed = dispatcher.dispatch_input(command, ui)
        outcome = dispatcher.execute(parsed.command, runtime, ui)
        assert outcome.ai_input is None
    assert not calls
    parsed = dispatcher.dispatch_input("/TEAM status", ui)
    dispatcher.execute(parsed.command, runtime, ui)
    assert calls == [("status",)]


def test_main_coordinator_skips_mcp_discovery(tmp_path, monkeypatch):
    paths, _ = setup_cli(tmp_path, monkeypatch, coordinator=True)
    config = SimpleNamespace(api_key_env="FIXTURE", workspace_root=str(paths.workspace_root),
        tool_timeout_seconds=1, command_timeout_seconds=1, permission_mode=PermissionMode.DEFAULT)
    monkeypatch.setattr(cli, "load_config", lambda p: config)
    monkeypatch.setattr(cli, "resolve_api_key", lambda name: "fixture-only")
    monkeypatch.setattr(cli, "DeepSeekProvider", lambda **kw: Provider())
    monkeypatch.setattr(cli, "load_permission_rules", lambda *a: None)
    monkeypatch.setattr(cli, "load_mcp_config", lambda *a: SimpleNamespace(servers={}, errors={}))
    closed = []
    monkeypatch.setattr(cli, "MCPRuntime", lambda: SimpleNamespace(shutdown=lambda: closed.append("runtime")))
    monkeypatch.setattr(cli, "MCPManager", lambda *a, **kw: SimpleNamespace(
        discover_all=lambda: pytest.fail("coordinator 不得 discovery"), shutdown=lambda: closed.append("mcp")))
    monkeypatch.setattr(cli, "SessionArchive", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "MemoryStore", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "MemoryService", lambda *a, **kw: SimpleNamespace(shutdown=lambda: None))
    monkeypatch.setattr(cli, "run_conversation", lambda **kw: 0)
    assert cli.main([]) == 0 and closed == ["mcp", "runtime"]


def test_detach_has_one_second_budget_even_when_cleanup_does_not_return(tmp_path, monkeypatch):
    import threading
    import time
    paths, runtime = setup_cli(tmp_path, monkeypatch)
    release = threading.Event()
    scope = SimpleNamespace(member_id="alice")
    runner = SimpleNamespace(scope=scope, close=lambda: True)
    backend = SimpleNamespace(request_stop=lambda s: release.wait(10), close=lambda **kw: None,
                              join_until=lambda deadline: None)
    runtime._handles["alice"] = (runner, backend)
    start = time.monotonic()
    try:
        assert runtime.detach() == 1
        assert time.monotonic() - start < 1.4
        from newcode.teams.types import TeamError
        with pytest.raises(TeamError, match="team_member_busy"):
            runtime.command(("lead",))
    finally:
        release.set()
        for job in runtime._closers.values():
            job["thread"].join(2)


def test_cli_real_member_agentloop_through_fake_factory(tmp_path, monkeypatch):
    import time
    from newcode.providers.base import ToolCallEvent, TextDelta
    from newcode.teams.paths import TeamPaths
    from newcode.teams.runtime import TeamRunnerFactory
    from newcode.teams.member import MemberPermissions
    from newcode.subagents.types import AgentCatalog
    from newcode.tools.registry import create_default_registry
    from newcode.tools.types import ToolCall
    from test_subagents_worktrees import _repository, _definition
    from test_teams_members import Factory, Provider as ChildProvider
    repo, manager = _repository(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    config = TeamConfig(enabled=True)
    monkeypatch.setattr(cli, "load_team_config", lambda *a, **kw: TeamConfigResult(config))
    monkeypatch.setattr(cli, "load_hook_rules", lambda *a, **kw: SimpleNamespace(rules=(), diagnostics=()))
    monkeypatch.delenv("NEWCODE_TEAM_COORDINATOR", raising=False)
    store = TeamStore(TeamPaths(repo, common_git_dir=repo / ".git", user_home=home))
    child_provider = ChildProvider()
    factory = Factory(child_provider)
    runtime = TeamAttachment(store=store, config=config, runner_factory=TeamRunnerFactory(
        registry=create_default_registry(), catalog=AgentCatalog((_definition(),)), provider_factory=factory,
        worktree_manager=manager, permissions=MemberPermissions(), default_model="fixture"))
    class LeadProvider:
        def __init__(self):
            self.requests = 0
        def stream_chat(self, messages, tools=None, allow_tool_calls=True):
            self.requests += 1
            if self.requests == 1:
                yield ToolCallEvent([ToolCall("create", "team_manage", {"operation": "create", "team_name": "fixture"})])
            elif self.requests == 2:
                yield ToolCallEvent([ToolCall("task", "team_tasks", {"operation": "create", "title": "规划", "description": "", "dependencies": []})])
            elif self.requests == 3:
                task = store.load_records(runtime.team_id, "tasks")[0]
                yield ToolCallEvent([ToolCall("spawn", "team_manage", {"operation": "spawn", "member_name": "alice",
                    "agent_name": "worker", "task_id": task["task_id"], "expected_team_revision": store.load_team(runtime.team_id).revision})])
            else:
                yield TextDelta("已派生")
    count = 0
    def read(prompt):
        nonlocal count
        count += 1
        if count == 1:
            return "/team lead"
        if count == 2:
            return "委派"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                records = store.load_records(runtime.team_id, "members")
            except cli.TeamError as exc:
                if exc.code != "team_store_busy":
                    raise
                time.sleep(0.05)
                continue
            members = [m for m in records if m["role"] == "member"]
            if members and members[0]["state"] in {"idle", "failed"}:
                assert members[0]["state"] == "idle"
                break
            time.sleep(0.02)
        else:
            pytest.fail("成员未限时进入 idle")
        return "/exit"
    output = StringIO()
    session = ChatSession()
    assert cli.run_conversation(LeadProvider(), session, tool_context=ToolContext(repo), input_func=read,
        output=output, error_output=output, team_attachment=runtime, worktree_manager=manager, agent_user_home=home,
        permission_manager=PermissionManager(mode=PermissionMode.TRUSTED)) == 0
    assert factory.created == [("fixture", 30)] and child_provider.requests
    assert child_provider.closed and runtime.role == "ordinary" and not runtime._retired
    assert (repo / "README.md").read_text() == "base contents\n"
    assert all("fixture-secret" not in (m.content or "") for m in session.messages)


def test_team_slash_only_attaches_git_binding_is_preflight(tmp_path, monkeypatch):
    paths, runtime = setup_cli(tmp_path, monkeypatch)
    runtime.store = None
    calls = []
    git = SimpleNamespace(common_git_dir=lambda root: calls.append("common") or paths.common_git_dir,
                          repository_root=lambda root: calls.append("root") or paths.workspace_root)
    manager = SimpleNamespace(_git=git)
    script = iter(["/team lead", "/team status", "/team close", "/exit"])
    def read(prompt):
        assert calls == ["common", "root"]
        return next(script)
    assert cli.run_conversation(Provider(), ChatSession(), tool_context=ToolContext(paths.workspace_root),
        agent_user_home=paths.home, input_func=read, output=StringIO(), error_output=StringIO(),
        team_attachment=runtime, worktree_manager=manager) == 0
    assert calls == ["common", "root"]


def test_role_switch_rebinds_skill_loader_to_final_registry(tmp_path, monkeypatch):
    import json
    from test_skills_tool import _skill
    from test_cli_commands import BatchProvider
    from newcode.providers.base import ToolCallEvent, TextDelta
    from newcode.tools.types import ToolCall
    paths, runtime = setup_cli(tmp_path, monkeypatch)
    _skill(paths.workspace_root, tools=("team_manage",))
    provider = BatchProvider([[TextDelta("普通")],
        [ToolCallEvent([ToolCall("load", "load_skill", {"name": "demo", "parameters": {}})])],
        [TextDelta("已加载")]])
    script = iter(["普通文本", "/team lead", "加载", "/exit"])
    session = ChatSession()
    assert cli.run_conversation(provider, session, tool_context=ToolContext(paths.workspace_root),
        agent_user_home=paths.home, input_func=lambda _: next(script), output=StringIO(), error_output=StringIO(),
        team_attachment=runtime, permission_manager=PermissionManager(mode=PermissionMode.TRUSTED)) == 0
    result = next(m for m in session.messages if m.role == "tool")
    assert json.loads(result.content)["ok"] is True
    assert "team_manage" in {t["function"]["name"] for t in provider.calls[-1][1]}
