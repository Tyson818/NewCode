"""P0/Pt/At 分离、coordinator 封闭集合及真实成员 gate。"""
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from newcode.agent.mode import AgentMode
from newcode.permissions.rules import parse_permission_rules_document
from newcode.permissions.types import PermissionLayer
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.teams.config import TeamConfig
from newcode.teams.coordinator import CoordinatorPolicy, COORDINATOR_TOOLS
from newcode.teams.tools import TEAM_TOOLS, MEMBER_TOOLS, register_team_tools
from newcode.teams.runtime import TeamAttachment
from newcode.teams.member import TeamMemberRunner, MemberPermissions
from newcode.teams.tasks import TaskService
from newcode.teams.approvals import ApprovalService
from newcode.tools.types import ToolCall, ToolResult, ToolSpec
from test_teams_members import setup_member, tool_names
from test_agent_loop_teams import make_loop, Provider


@pytest.mark.parametrize("enabled,flag,env,expected", [
    (False, True, "1", False), (True, False, "1", False), (True, True, "0", False),
    (True, True, "true", False), (True, True, "1", True)])
def test_coordinator_requires_both_user_authorization_and_exact_environment(enabled, flag, env, expected):
    config = TeamConfig(enabled=enabled, coordinator_enabled=flag)
    policy = CoordinatorPolicy.from_config(config, {"NEWCODE_TEAM_COORDINATOR": env})
    assert policy.enabled is expected


@pytest.mark.parametrize("name", ["write_file", "replace_in_file", "run_command", "agent", "load_skill", "mcp__fixture", "unknown"])
def test_coordinator_schema_and_old_tool_call_cannot_bypass(tmp_path, name):
    loop, runtime, provider = make_loop(tmp_path, [[ToolCall("bad", name, {})]], coordinator=CoordinatorPolicy(True))
    calls = []
    if loop.registry.get(name) is None:
        tool = SimpleNamespace(spec=ToolSpec(name, "fixture", {"type": "object"}),
                               run=lambda *args: calls.append(args) or ToolResult.success(name))
        loop.registry.register(tool, do_visible=True, read_only=False)
    list(loop.run("执行"))
    assert name not in {t["function"]["name"] for t in provider.requests[0][1]}
    assert not calls and not tuple(runtime.store.paths.root.glob("team-*"))
    assert json.loads(next(m.content for m in loop.session.messages if m.role == "tool"))["error"]["code"] == "disallowed_tool"


def enabled_member(tmp_path, callback, *, approval=True):
    _, kwargs, provider = setup_member(tmp_path, callback, approval=approval)
    rules = parse_permission_rules_document({"rules": [
        {"id": name, "tool": name, "action": "allow", "reason": "fixture", "risk_level": "medium",
         "match": {"path_glob": "**"} if name == "write_file" else {"command_glob": name + " *"}}
        for name in [*MEMBER_TOOLS, "write_file"]
    ]}, source=PermissionLayer.PROJECT_RULES)
    policy = kwargs["policy"]
    policy = replace(policy, startup_tools=policy.startup_tools | MEMBER_TOOLS, current_tools=policy.current_tools | MEMBER_TOOLS)
    kwargs.update(policy=policy, permissions=MemberPermissions(project_rules=rules.rules))
    runner = TeamMemberRunner(**kwargs, enable_team_tools=True)
    return runner, provider


def test_member_submit_lead_approve_then_revision_revokes_inflight_write(tmp_path):
    holder = {}
    def callback(provider, messages, tools):
        if len(provider.requests) == 1:
            yield ToolCallEvent([ToolCall("plan", "team_tasks", {"operation": "submit_plan",
                                    "task_id": holder["task"].task_id, "content": "先规划再修改"})])
        elif len(provider.requests) == 3:
            yield ToolCallEvent([ToolCall("approved-write", "write_file", {"path": "README.md", "content": "已批准"})])
        elif len(provider.requests) == 5:
            # 此响应按已批准 revision 构造；返回调用前先修订计划。
            plan = holder["plan"]
            ApprovalService(holder["runner"].store, holder["runner"].scope.team_id, "alice").submit(
                holder["task"].task_id, "修改后的计划", plan_id=plan.plan_id, expected_plan_revision=plan.revision)
            yield ToolCallEvent([ToolCall("old-write", "write_file", {"path": "README.md", "content": "不得执行"})])
        else:
            yield TextDelta("等待审批")
    runner, provider = enabled_member(tmp_path, callback)
    holder["runner"] = runner
    tasks = TaskService(runner.store, runner.scope.team_id)
    task = tasks.create("lead", title="修改")
    holder["task"] = tasks.assign("lead", task.task_id, "alice", expected_revision=task.revision)
    try:
        assert runner.activate().natural
        assert MEMBER_TOOLS <= tool_names(provider)
        assert "write_file" not in tool_names(provider)
        plan_dict = runner.store.load_records(runner.scope.team_id, "plans")[0]
        from newcode.teams.types import TeamPlan
        holder["plan"] = TeamPlan.from_dict(plan_dict)
        runtime = TeamAttachment(store=runner.store, config=TeamConfig(enabled=True))
        runtime.command(("open", runner.scope.team_id))
        wake = []
        runtime._handles["alice"] = (runner, SimpleNamespace(wake=lambda scope: wake.append(scope)))
        from newcode.agent import AgentLoop
        from newcode.permissions.manager import PermissionManager
        from newcode.permissions.types import PermissionMode
        from newcode.session import ChatSession
        from newcode.tools.registry import create_default_registry
        from newcode.tools.types import ToolContext
        registry = create_default_registry()
        register_team_tools(registry, runtime)
        provider_lead = Provider([[ToolCall("approve", "team_manage", {"operation": "approve_plan",
            "plan_id": plan_dict["plan_id"], "expected_plan_revision": 1})]])
        lead = AgentLoop(provider=provider_lead, session=ChatSession(), registry=registry,
                         tool_context=ToolContext(runner.store.paths.workspace_root),
                         permission_manager=PermissionManager(mode=PermissionMode.TRUSTED), team_runtime=runtime)
        list(lead.run("审批"))
        assert wake and runner._policy.plan_state.value == "approved"
        assert runner.permission.session_rules.rules == ()
        assert runner.activate().natural
        assert "write_file" in tool_names(provider, 2)
        assert (runner.tool_context.cwd / "README.md").read_text(encoding="utf-8") == "已批准"
        from newcode.teams.types import TeamError
        with pytest.raises(TeamError, match="team_worker_failed"):
            runner.activate()
        assert (runner.tool_context.cwd / "README.md").read_text(encoding="utf-8") == "已批准"
        result = next(m for m in runner.session.messages if m.role == "tool" and m.tool_call_id == "old-write")
        assert not json.loads(result.content)["ok"]
        assert runner._policy.startup_tools == runner._policy.current_tools
    finally:
        runner.close()


def test_member_coordination_requires_permission_without_main_confirmer(tmp_path):
    holder = {}
    def callback(provider, messages, tools):
        if len(provider.requests) == 1:
            yield ToolCallEvent([ToolCall("send", "team_message", {"operation": "send", "recipient": "lead", "body": "hi", "idempotency_key": "k"})])
        else:
            yield TextDelta("完成")
    runner, provider = enabled_member(tmp_path, callback, approval=False)
    runner.permissions = MemberPermissions()
    try:
        runner.activate()
        result = next(m for m in runner.session.messages if m.role == "tool")
        assert json.loads(result.content)["error"]["code"] == "permission_denied"
        assert not any(m["body"] == "hi" for m in runner.store.load_records(runner.scope.team_id, "messages"))
        assert runner.permission.confirmer.__class__.__name__ == "DenyByDefaultConfirmer"
    finally:
        runner.close()


@pytest.mark.parametrize("narrow", ["plan", "skill", "permission"])
def test_running_member_rechecks_main_thread_policy_before_old_write(tmp_path, narrow):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from newcode.permissions.manager import PermissionManager
    from newcode.permissions.types import PermissionMode
    from newcode.teams.types import TeamError
    ready, release = threading.Event(), threading.Event()
    def callback(provider, messages, tools):
        if len(provider.requests) == 1:
            ready.set()
            assert release.wait(30)
            yield ToolCallEvent([ToolCall("old", "write_file", {"path": "README.md", "content": "禁止"})])
        else:
            yield TextDelta("结束")
    runner, provider = enabled_member(tmp_path, callback, approval=False)
    runtime = TeamAttachment(store=runner.store, config=TeamConfig(enabled=True))
    runtime.command(("open", runner.scope.team_id))
    runtime._handles["alice"] = (runner, SimpleNamespace())
    names = runner._policy.startup_tools
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runner.activate)
            assert ready.wait(60)
            if narrow == "permission":
                runtime.publish_permissions(PermissionManager(mode=PermissionMode.STRICT))
            else:
                allowed = names - {"write_file", "replace_in_file"}
                runtime.publish(allowed, AgentMode.PLAN if narrow == "plan" else AgentMode.DO)
            release.set()
            try:
                future.result(timeout=30)
            except TeamError:
                pass
        assert (runner.tool_context.cwd / "README.md").read_text() == "base contents\n"
        result = next(m for m in runner.session.messages if m.role == "tool")
        assert json.loads(result.content)["error"]["code"] in {"permission_denied", "disallowed_tool"}
    finally:
        release.set()
        runner.close()


def test_pause_stale_revision_leaves_old_data_and_cancel_unchanged(tmp_path):
    import threading
    from test_teams_approvals import setup_approval
    from newcode.teams.types import TeamError
    _, store, team, _ = setup_approval(tmp_path)
    runner = object.__new__(TeamMemberRunner)
    runner.store, runner.scope = store, SimpleNamespace(team_id=team.team_id, member_id="alice")
    runner.cancel = threading.Event()
    before = store.load_records(team.team_id, "members")
    with pytest.raises(TeamError, match="team_revision_conflict"):
        runner.pause(expected_revision=999)
    assert not runner.cancel.is_set() and store.load_records(team.team_id, "members") == before


def test_policy_disk_busy_publishes_narrowing_and_fails_approval_closed():
    from newcode.teams.types import TeamPolicySnapshot, TeamScope, TeamError, PlanState
    scope = TeamScope("team-" + "a" * 32, "alice", "20261016-101010-ab12", 1)
    policy = TeamPolicySnapshot(scope, 1, frozenset({"read_file", "write_file"}),
        frozenset({"read_file", "write_file"}), plan_state=PlanState.APPROVED, plan_revision=1)
    def busy(*args):
        raise TeamError("team_store_busy")
    runtime = TeamAttachment(store=SimpleNamespace(load_records=busy), config=TeamConfig(enabled=True))
    runtime.role, runtime.team_id = "lead", scope.team_id
    runner = SimpleNamespace(_policy=policy)
    def publish(snapshot):
        runner._policy = snapshot
    runner.publish_policy = publish
    runtime._handles["alice"] = (runner, None)
    runtime.publish({"read_file"}, AgentMode.PLAN)
    assert runner._policy.current_tools == {"read_file"}
    assert runner._policy.plan_state is PlanState.PENDING and runner.mode is AgentMode.PLAN


def test_closed_scope_cannot_start_provider(tmp_path):
    import threading
    from newcode.teams.types import TeamError
    _, kwargs, _ = setup_member(tmp_path)
    runner = TeamMemberRunner(**kwargs)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(TeamError, match="team_state_invalid"):
        runner.activate(cancel_event=cancelled)
    assert not kwargs["provider_factory"].created
    assert runner._owner is None


def test_archived_team_cannot_execute_queued_member_wake(tmp_path):
    from newcode.teams.types import TeamError, RecordChange, new_id
    runner, _, provider = setup_member(tmp_path)
    try:
        runner.activate()
        requests = len(provider.requests)
        # 模拟 idle 静止屏障后完成 archive，而此前已排队的唤醒随后到达。
        for state in ("closing", "closed"):
            team = runner.store.load_team(runner.scope.team_id)
            changed = replace(team, state=state, revision=team.revision + 1)
            runner.store.commit_transaction(team.team_id, expected_revision=team.revision, mutation_id=new_id("mutation"),
                changes=(RecordChange("team", team.team_id, changed.to_dict()),))
        with pytest.raises(TeamError, match="team_state_invalid"):
            runner.activate()
        assert len(provider.requests) == requests
        assert (runner.tool_context.cwd / "README.md").read_text() == "base contents\n"
    finally:
        assert runner.close()


@pytest.mark.parametrize("denied", sorted(MEMBER_TOOLS))
def test_definition_deny_has_priority_over_role_delegation(denied):
    from newcode.teams.types import TeamScope, TeamPolicySnapshot, TeamMemberRecord, MemberState
    scope = TeamScope("team-" + "a" * 32, "alice", "20261016-101010-ab12", 1)
    tools = MEMBER_TOOLS | {"read_file", "write_file"}
    runner = object.__new__(TeamMemberRunner)
    runner.scope = scope
    runner.registry = SimpleNamespace(names=lambda: tools)
    runner._policy = TeamPolicySnapshot(scope, 1, tools, tools)
    runner.definition = SimpleNamespace(tools_allow=("read_file", "write_file"), tools_deny=("write_file", denied))
    runner.mode = AgentMode.DO
    runner.skill_state = SimpleNamespace(activations=())
    runner.team_runtime = object()
    member = TeamMemberRecord("alice", "alice", agent_name="reader", runtime_generation=1,
                              state=MemberState.RUNNING, requires_plan_approval=False)
    allowed = runner._allowed_locked(member, {"team": {scope.team_id: {"state": "active"}}})
    assert denied not in allowed and "write_file" not in allowed
    assert allowed == {"read_file"} | (MEMBER_TOOLS - {denied})
