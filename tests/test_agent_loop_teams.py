"""Team 工具经真实 AgentLoop gate 与 executor，而非直接调用服务。"""
import json

import pytest

from newcode.agent import AgentLoop
from newcode.agent.mode import AgentMode
from newcode.permissions.manager import PermissionManager
from newcode.permissions.rules import parse_permission_rules_document
from newcode.permissions.types import PermissionMode, PermissionLayer
from newcode.providers.base import ToolCallEvent, TextDelta
from newcode.session import ChatSession
from newcode.teams.config import TeamConfig
from newcode.teams.runtime import TeamAttachment
from newcode.teams.store import TeamStore
from newcode.teams.tools import register_team_tools, TeamTool
from newcode.tools.registry import create_default_registry
from newcode.tools.types import ToolCall, ToolContext
from test_teams_paths import make_paths


class Provider:
    def __init__(self, calls):
        self.calls = calls
        self.requests = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.requests.append((list(messages), tools))
        if len(self.requests) <= len(self.calls):
            yield ToolCallEvent(self.calls[len(self.requests)-1])
        else:
            yield TextDelta("完成")


def make_loop(tmp_path, calls, *, permission=None, coordinator=None):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    runtime = TeamAttachment(store=store, config=TeamConfig(enabled=True), coordinator=coordinator)
    runtime.command(("lead",))
    registry = create_default_registry()
    register_team_tools(registry, runtime)
    provider = Provider(calls)
    session = ChatSession()
    loop = AgentLoop(provider=provider, session=session, registry=registry, tool_context=ToolContext(paths.workspace_root),
        permission_manager=permission or PermissionManager(mode=PermissionMode.TRUSTED), team_runtime=runtime)
    return loop, runtime, provider


@pytest.mark.parametrize("mode,permission", [(AgentMode.PLAN, PermissionManager(mode=PermissionMode.TRUSTED)),
                                            (AgentMode.DO, PermissionManager())])
def test_denied_create_zero_store_and_worktree_mutation(tmp_path, mode, permission):
    call = ToolCall("c", "team_manage", {"operation": "create", "team_name": "test"})
    loop, runtime, provider = make_loop(tmp_path, [[call]], permission=permission)
    list(loop.run("执行", mode=mode))
    assert runtime.team_id is None and not tuple(runtime.store.paths.root.glob("team-*"))
    result = json.loads(next(m.content for m in loop.session.messages if m.role == "tool"))
    assert result["error"]["code"] in {"disallowed_tool", "permission_denied"}


def test_success_create_task_and_fixed_result_order(tmp_path):
    loop, runtime, provider = make_loop(tmp_path, [
        [ToolCall("create", "team_manage", {"operation": "create", "team_name": "test"})],
        [ToolCall("one", "team_tasks", {"operation": "create", "title": "一", "description": "", "dependencies": []}),
         ToolCall("two", "team_tasks", {"operation": "create", "title": "二", "description": "", "dependencies": []})]])
    list(loop.run("开始"))
    results = [m for m in loop.session.messages if m.role == "tool"]
    assert [m.tool_call_id for m in results] == ["create", "one", "two"]
    assert all(json.loads(m.content)["ok"] for m in results)
    assert {t["title"] for t in runtime.store.load_records(runtime.team_id, "tasks")} == {"一", "二"}


def test_explicit_permission_deny_precedes_store(tmp_path):
    rules = parse_permission_rules_document({"rules": [{"id": "deny", "tool": "team_manage", "action": "deny",
        "reason": "fixture", "risk_level": "medium", "match": {"command_glob": "team_manage *"}}]},
                                           source=PermissionLayer.PROJECT_RULES)
    permission = PermissionManager(mode=PermissionMode.TRUSTED, project_rules=rules)
    loop, runtime, _ = make_loop(tmp_path, [[ToolCall("c", "team_manage", {"operation": "create", "team_name": "x"})]], permission=permission)
    list(loop.run("开始"))
    assert runtime.team_id is None and not tuple(runtime.store.paths.root.glob("team-*"))


def test_direct_tool_call_without_permission_admission_cannot_write(tmp_path):
    loop, runtime, _ = make_loop(tmp_path, [])
    result = TeamTool("team_manage", runtime).run({"operation": "create", "team_name": "x"}, loop.tool_context)
    assert not result.ok and result.error.code == "permission_denied"
    assert not tuple(runtime.store.paths.root.glob("team-*"))


def test_permission_change_between_precheck_and_executor_is_rejected(tmp_path):
    call = ToolCall("c", "team_manage", {"operation": "create", "team_name": "x"})
    loop, runtime, _ = make_loop(tmp_path, [])
    runtime.publish(loop._visible_tool_names(AgentMode.DO), AgentMode.DO)
    assert loop._precheck_permissions([call])[1] == [call]
    loop.permission_manager.mode = PermissionMode.STRICT
    result = loop._execute_tool_call(call)
    assert not result.ok and result.error.code == "permission_denied" and not tuple(runtime.store.paths.root.glob("team-*"))


def test_active_skill_cannot_grant_team_create(tmp_path):
    from test_skills_tool import _skill
    from newcode.skills.discovery import SkillDiscovery
    from newcode.skills.loader import SkillLoader
    call = ToolCall("c", "team_manage", {"operation": "create", "team_name": "x"})
    loop, runtime, provider = make_loop(tmp_path, [[call]])
    _skill(loop.tool_context.workspace_root)
    catalog = SkillDiscovery().discover(loop.tool_context.workspace_root)
    loop.skill_state.activate(SkillLoader().load("demo", {}, catalog, available_tools=loop.registry.names()))
    list(loop.run("不得创建"))
    assert runtime.team_id is None and not tuple(runtime.store.paths.root.glob("team-*"))
    assert "team_manage" not in {t["function"]["name"] for t in provider.requests[0][1]}


def test_unimplemented_integration_has_no_shell_fallback(tmp_path):
    loop, runtime, provider = make_loop(tmp_path, [
        [ToolCall("create", "team_manage", {"operation": "create", "team_name": "test"})],
        [ToolCall("preview", "team_integrate", {"operation": "preview", "target_member_id": "alice", "expected_member_revision": 1})]])
    list(loop.run("预览"))
    result = json.loads(next(m.content for m in loop.session.messages if m.role == "tool" and m.tool_call_id == "preview"))
    assert not result["ok"] and result["error"]["code"] == "team_coordinator_unavailable"
    assert not tuple(loop.tool_context.workspace_root.glob(".newcode/worktrees/*"))
