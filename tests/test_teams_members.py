"""临时本地仓库 + fake Provider，经过真实 AgentLoop 的成员验收。"""

from dataclasses import replace
import json
import threading
import time

import pytest

from newcode.agent.mode import AgentMode
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.permissions.types import PermissionMode
from newcode.session import ChatMessage
from newcode.teams.member import TeamMemberRunner, MemberPermissions
from newcode.teams.paths import TeamPaths
from newcode.teams.store import TeamStore
from newcode.teams.types import (MemberState, MessageKind, PlanState, RecordChange,
                                TeamError, TeamMemberRecord, TeamPolicySnapshot, TeamScope, new_id)
from newcode.tools.registry import create_default_registry
from newcode.tools.types import ToolCall
from test_subagents_worktrees import _repository, _definition


class Provider:
    def __init__(self, callback=None):
        self.requests = []
        self.callback = callback
        self.closed = False

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.requests.append((tuple(messages), tuple(tools or ())))
        if self.callback:
            yield from self.callback(self, messages, tools)
        else:
            yield TextDelta("完成 fixture-secret")

    def close(self):
        self.closed = True


class Factory:
    available_models = ("fixture",)

    def __init__(self, provider):
        self.provider = provider
        self.created = []

    def create(self, model, *, timeout_seconds):
        self.created.append((model, timeout_seconds))
        return self.provider


def setup_member(tmp_path, callback=None, approval=False, permissions=None):
    repo, manager = _repository(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    store = TeamStore(TeamPaths(repo, common_git_dir=repo / ".git", user_home=home), sensitive_values=("fixture-secret",))
    team = store.create_team("成员测试")
    record = TeamMemberRecord("alice", "alice", agent_name="worker", requires_plan_approval=approval, auto_wake=True)
    store.commit_transaction(team.team_id, expected_revision=team.revision, mutation_id=new_id("mutation"),
                             changes=(RecordChange("members", record.member_id, record.to_dict()),))
    registry = create_default_registry()
    scope = TeamScope(team.team_id, "alice", "20261016-101010-ab12", 1)
    policy = TeamPolicySnapshot(scope, 1, frozenset(registry.names()), frozenset(registry.names()))
    provider = Provider(callback)
    factory = Factory(provider)
    kwargs = dict(store=store, scope=scope, definition=_definition(), registry=registry,
                  provider_factory=factory, default_model="fixture", policy=policy, worktree_manager=manager,
                  permissions=permissions or MemberPermissions())
    return TeamMemberRunner(**kwargs), kwargs, provider


def member_record(runner):
    return next(m for m in runner.store.load_team(runner.scope.team_id).roster if m.member_id == "alice")


def tool_names(provider, index=0):
    return {item["function"]["name"] for item in provider.requests[index][1]}


def test_natural_final_checkpoint_idle_and_independent_state(tmp_path):
    runner, _, provider = setup_member(tmp_path)
    try:
        result = runner.activate("开始")
        assert result.natural and "fixture-secret" not in result.summary
        record = member_record(runner)
        assert record.state is MemberState.IDLE and record.base_commit
        assert record.input_tokens > 0 and record.output_tokens > 0
        restored = runner.checkpoints.restore(record.checkpoint_session_id)
        assert restored.session.messages[-1].role == "assistant"
        assert "fixture-secret" not in restored.session.messages[-1].content
        assert runner.permission.session_rules.rules == ()
        assert runner.skill_state.activations == ()
        assert runner.context.session is runner.session
        assert runner.tool_context.cwd != runner.store.paths.workspace_root
        assert not {"agent", "load_skill", "run_command"} & tool_names(provider)
        messages = runner.store.load_records(runner.scope.team_id, "messages")
        assert len(messages) == 1 and messages[0]["kind"] == MessageKind.MEMBER_IDLE.value
    finally:
        assert runner.close()
    assert provider.closed


def test_confirmation_denied_zero_write_and_no_main_confirmer(tmp_path):
    def callback(provider, messages, tools):
        if len(provider.requests) == 1:
            yield ToolCallEvent([ToolCall("write", "write_file", {"path": "README.md", "content": "changed"})])
        else:
            yield TextDelta("被拒绝后结束")
    runner, _, provider = setup_member(tmp_path, callback)
    try:
        runner.activate()
        assert (runner.tool_context.cwd / "README.md").read_text() == "base contents\n"
        tool_result = next(m for m in runner.session.messages if m.role == "tool")
        assert json.loads(tool_result.content)["error"]["code"] == "permission_denied"
        assert runner.permission.confirmer.__class__.__name__ == "DenyByDefaultConfirmer"
        assert not runner.permission.session_rules.rules
    finally:
        runner.close()


def test_restore_invalid_and_unavailable_worktree_never_create_provider(tmp_path):
    runner, kwargs, _ = setup_member(tmp_path)
    runner.worktrees = None
    with pytest.raises(TeamError):
        runner.activate()
    assert kwargs["provider_factory"].created == []
    assert runner.session is None
    runner.close()


def test_checkpoint_failure_cannot_publish_idle_or_notification(tmp_path):
    runner, _, _ = setup_member(tmp_path)
    def fail(point):
        if point == "member_after_final_checkpoint":
            raise TeamError("team_store_unavailable")
    runner.store._fault = fail
    try:
        with pytest.raises(TeamError):
            runner.activate()
        assert member_record(runner).state is not MemberState.IDLE
        assert runner.store.load_records(runner.scope.team_id, "messages") == ()
    finally:
        runner.close()


def reopened(runner, kwargs, *, generation=2, factory=None):
    from newcode.worktrees.manager import WorktreeManager
    scope = replace(runner.scope, generation=generation)
    return TeamMemberRunner(**dict(kwargs, scope=scope, policy=replace(kwargs["policy"], scope=scope),
                                  provider_factory=factory or Factory(Provider()),
                                  worktree_manager=WorktreeManager(empty_hooks_dir=runner.store.paths.workspace_root.parent / "manager-empty-hooks")))


def test_cross_generation_restore_rebuilds_every_runtime_state(tmp_path):
    runner, kwargs, _ = setup_member(tmp_path)
    runner.activate("原上下文")
    old = (runner.session, runner.context, runner.permission, runner.skill_state, runner.cache, runner.cancel)
    sid = member_record(runner).checkpoint_session_id
    assert runner.close()
    child = reopened(runner, kwargs)
    try:
        child.open()
        new = (child.session, child.context, child.permission, child.skill_state, child.cache, child.cancel)
        assert all(a is not b for a, b in zip(old, new))
        assert child.session.session_id == sid
        assert any(m.content == "原上下文" for m in child.session.messages)
        assert child.permission.session_rules.rules == ()
        assert child.skill_state.activations == ()
        assert child.context.artifacts.workspace_root == child.tool_context.cwd
        assert member_record(child).runtime_generation == 2
        child.activate("继续")
    finally:
        child.close()


def test_owner_busy_and_stale_generation_never_create_second_provider(tmp_path):
    runner, kwargs, _ = setup_member(tmp_path)
    runner.open()
    candidate = reopened(runner, kwargs)
    with pytest.raises(TeamError, match="team_member_busy"):
        candidate.open()
    assert not candidate.factory.created
    runner.close()
    same = reopened(runner, kwargs, generation=1)
    with pytest.raises(TeamError, match="team_scope_invalid"):
        same.open()
    assert not same.factory.created


@pytest.mark.parametrize("bad", ["corrupt", "expired", "budget", "workspace", "integrated"])
def test_invalid_restore_preserves_checkpoint_and_never_starts_provider(tmp_path, bad):
    runner, kwargs, _ = setup_member(tmp_path)
    runner.activate()
    sid = runner.session.session_id
    path = runner.store.paths.root.joinpath(*runner.checkpoints._parts(sid))
    runner.close()
    if bad == "corrupt":
        path.write_bytes(path.read_bytes() + b"broken\n")
    elif bad == "expired":
        lines = path.read_text(encoding="utf-8").splitlines()
        metadata = json.loads(lines[0]); metadata["updated_at"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(metadata) + "\n" + "\n".join(lines[1:]) + "\n", encoding="utf-8")
    elif bad == "budget":
        runner.session.messages.append(ChatMessage("user", "x" * 210_000))
        runner.checkpoints.checkpoint(runner.session)
    elif bad == "workspace":
        lines = path.read_text(encoding="utf-8").splitlines()
        metadata = json.loads(lines[0]); metadata["team_checkpoint"]["workspace_fingerprint"] = "0" * 64
        path.write_text(json.dumps(metadata)+"\n"+"\n".join(lines[1:])+"\n", encoding="utf-8")
    else:
        record = member_record(runner)
        team = runner.store.load_team(runner.scope.team_id)
        updated = replace(record, revision=record.revision+1, state=MemberState.INTEGRATED)
        runner.store.commit_transaction(team.team_id, expected_revision=team.revision, mutation_id=new_id("mutation"),
                                        changes=(RecordChange("members", "alice", updated.to_dict()),))
    original = path.read_bytes()
    candidate = reopened(runner, kwargs)
    with pytest.raises(TeamError):
        candidate.open()
    assert not candidate.factory.created
    assert path.read_bytes() == original


def test_policy_narrowing_revalidates_old_model_tool_call_and_next_request(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def callback(provider, messages, tools):
        if len(provider.requests) == 1:
            entered.set()
            assert release.wait(10)
            yield ToolCallEvent([ToolCall("old", "write_file", {"path": "README.md", "content": "bad"})])
        else:
            yield TextDelta("已收窄")
    runner, kwargs, provider = setup_member(tmp_path, callback, permissions=MemberPermissions(mode=PermissionMode.TRUSTED))
    errors = []
    thread = threading.Thread(target=lambda: _capture(errors, runner.activate))
    thread.start()
    try:
        assert entered.wait(25)
        runner.publish_policy(replace(kwargs["policy"], revision=2, current_tools=frozenset({"read_file", "find_files", "search_code"})))
        release.set(); thread.join(25)
        assert not thread.is_alive() and not errors
        assert "write_file" in tool_names(provider, 0)
        assert "write_file" not in tool_names(provider, 1)
        assert (runner.tool_context.cwd / "README.md").read_text() == "base contents\n"
        result = next(m for m in runner.session.messages if m.role == "tool")
        assert json.loads(result.content)["error"]["code"] == "disallowed_tool"
        with pytest.raises(TeamError, match="team_policy_changed"):
            runner.publish_policy(replace(kwargs["policy"], revision=3))
    finally:
        release.set(); thread.join(25); runner.close()


def _capture(errors, callback):
    try:
        callback()
    except BaseException as exc:
        errors.append(exc)


def test_uncooperative_provider_cannot_be_replaced_or_report_quiescence(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def callback(provider, messages, tools):
        entered.set(); release.wait(25)
        yield TextDelta("done")
    runner, kwargs, _ = setup_member(tmp_path, callback)
    errors = []
    thread = threading.Thread(target=lambda: _capture(errors, runner.activate))
    thread.start()
    try:
        assert entered.wait(25)
        runner.pause()
        assert not runner.close() and not runner.quiescent()
        assert not member_record(runner).auto_wake
        with pytest.raises(TeamError, match="team_member_busy"):
            runner.resume()
        candidate = reopened(runner, kwargs)
        with pytest.raises(TeamError, match="team_member_busy"):
            candidate.open()
        assert not candidate.factory.created
    finally:
        release.set(); thread.join(25)
        assert runner.close()
    assert errors


def test_mail_wake_checkpoint_ack_and_generation_dedupe(tmp_path):
    from newcode.teams.mailbox import MailboxService
    from newcode.teams.backends import LightweightBackend
    runner, kwargs, provider = setup_member(tmp_path)
    backend = LightweightBackend(runner, max_workers=1)
    try:
        backend.start(runner.scope)
        _wait_idle(backend, runner.scope)
        mailbox = MailboxService(runner.store, runner.scope.team_id, "lead", wake=lambda _: backend.wake(runner.scope))
        message = mailbox.send("alice", "继续工作", idempotency_key="wake-test").messages[0]
        limit = time.monotonic()+30
        while len(provider.requests) < 2 and time.monotonic() < limit:
            time.sleep(.02)
        _wait_idle(backend, runner.scope)
        assert sum(message.message_id in (m.content or "") for m in runner.session.messages) == 1
        assert MailboxService(runner.store, runner.scope.team_id, "alice").read().messages == ()
    finally:
        backend.shutdown(); runner.close()
    candidate = reopened(runner, kwargs)
    try:
        candidate.activate()
        assert sum(message.message_id in (m.content or "") for m in candidate.session.messages) == 1
        proof = candidate.checkpoints.restore(candidate.scope.session_id).proof
        assert proof.generation == 2 and message.message_id in proof.processed_message_ids
    finally:
        candidate.close()


def _wait_idle(backend, scope):
    deadline = time.monotonic()+30
    while time.monotonic() < deadline:
        status = backend.status(scope)
        if status.state in {"idle", "failed"} and not status.executing:
            assert status.state == "idle", status.error_code
            return
        time.sleep(.02)
    pytest.fail("成员未在限定时间内进入 idle")


def test_approval_and_plan_mode_remove_writes_and_indirect_entrances(tmp_path):
    runner, _, provider = setup_member(tmp_path, approval=True)
    try:
        runner.activate()
        assert tool_names(provider) <= {"read_file", "find_files", "search_code"}
        assert member_record(runner).state is MemberState.IDLE
    finally:
        runner.close()


def test_plan_mode_and_empty_startup_ceiling_do_not_expand(tmp_path):
    runner, kwargs, provider = setup_member(tmp_path)
    runner.mode = AgentMode.PLAN
    runner.publish_policy(replace(kwargs["policy"], revision=2, current_tools=frozenset()))
    try:
        runner.activate()
        assert tool_names(provider) == set()
    finally:
        runner.close()


def test_sop_only_in_requests_and_no_indirect_runtime_instances(tmp_path, monkeypatch):
    import newcode.teams.member as module
    loops = []
    real_loop = module.AgentLoop
    class ObservedLoop(real_loop):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            loops.append(self)
    monkeypatch.setattr(module, "AgentLoop", ObservedLoop)
    runner, _, provider = setup_member(tmp_path)
    runner.definition = replace(runner.definition, body="PRIVATE SOP fixture-secret")
    try:
        runner.activate()
        loop = loops[0]
        assert loop.memory_service is None and loop.hook_engine is None
        assert loop.subagent_manager is None
        assert not {"agent", "load_skill", "run_command"} & set(loop.registry.names())
        assert not any(name.startswith("mcp__") for name in loop.registry.names())
        assert "PRIVATE SOP" in provider.requests[0][0][0].content
        raw = runner.store.paths.root.joinpath(*runner.checkpoints._parts(runner.session.session_id)).read_text(encoding="utf-8")
        assert "PRIVATE SOP" not in raw and "fixture-secret" not in raw
        assert not any("PRIVATE SOP" in (m.content or "") for m in runner.session.messages)
    finally:
        runner.close()


def test_pause_blocks_automatic_wake_until_explicit_resume(tmp_path):
    runner, _, provider = setup_member(tmp_path)
    try:
        runner.activate()
        runner.pause()
        with pytest.raises(TeamError, match="team_state_invalid"):
            runner.activate()
        assert len(provider.requests) == 1
        runner.resume()
        runner.activate("恢复")
        assert len(provider.requests) == 2
        assert member_record(runner).state is MemberState.IDLE
    finally:
        runner.close()


def test_checkpoint_before_ack_crash_recovers_once_in_new_generation(tmp_path, monkeypatch):
    from newcode.teams.mailbox import MailboxService
    runner, kwargs, provider = setup_member(tmp_path)
    runner.activate()
    message = MailboxService(runner.store, runner.scope.team_id, "lead").send("alice", "崩溃后继续", "crash-mail").messages[0]
    original_ack = MailboxService.ack
    def fail_ack(*args, **kwargs):
        raise TeamError("team_store_unavailable")
    monkeypatch.setattr(MailboxService, "ack", fail_ack)
    with pytest.raises(TeamError):
        runner.activate()
    assert len(provider.requests) == 1
    assert member_record(runner).state is MemberState.FAILED
    runner.close()
    monkeypatch.setattr(MailboxService, "ack", original_ack)
    candidate = reopened(runner, kwargs)
    try:
        candidate.resume()
        candidate.activate()
        assert sum(message.message_id in (m.content or "") for m in candidate.session.messages) == 1
        assert MailboxService(candidate.store, candidate.scope.team_id, "alice").read().messages == ()
    finally:
        candidate.close()


def test_unsupported_model_never_falls_back_or_runs_provider(tmp_path):
    runner, _, _ = setup_member(tmp_path)
    runner.definition = replace(runner.definition, model="unavailable")
    with pytest.raises(TeamError, match="team_backend_unavailable"):
        runner.open()
    assert runner.factory.created == []
    assert runner.session is None
    assert member_record(runner).worktree_relative
    runner.close()


def test_real_loop_writes_only_member_root_and_quiescence_is_a_barrier(tmp_path):
    def callback(provider, messages, tools):
        if len(provider.requests) == 1:
            yield ToolCallEvent([ToolCall("own", "write_file", {"path": "README.md", "content": "own result"})])
        elif len(provider.requests) == 2:
            yield ToolCallEvent([ToolCall("escape", "write_file", {"path": str(tmp_path / "repository" / "README.md"), "content": "bad"})])
        else:
            yield TextDelta("结束")
    runner, _, _ = setup_member(tmp_path, callback, permissions=MemberPermissions(mode=PermissionMode.TRUSTED))
    try:
        runner.activate()
        assert (runner.tool_context.cwd / "README.md").read_text() == "own result"
        assert (runner.store.paths.workspace_root / "README.md").read_text() == "base contents\n"
        results = [json.loads(m.content) for m in runner.session.messages if m.role == "tool"]
        assert results[0]["ok"] and results[1]["error"]["code"] == "permission_denied"
        with runner.quiescence():
            with pytest.raises(TeamError, match="team_member_busy"):
                runner.activate()
    finally:
        runner.close()


def test_lightweight_pause_mail_and_explicit_resume_do_not_stop_or_duplicate_owner(tmp_path):
    from newcode.teams.backends import LightweightBackend
    from newcode.teams.mailbox import MailboxService
    entered, release = threading.Event(), threading.Event()
    def callback(provider, messages, tools):
        if len(provider.requests) == 1:
            entered.set(); assert release.wait(25)
        yield TextDelta("done")
    runner, _, provider = setup_member(tmp_path, callback)
    backend = LightweightBackend(runner, max_workers=1)
    try:
        backend.start(runner.scope)
        assert entered.wait(25)
        runner.pause(); release.set()
        _wait_idle(backend, runner.scope)
        assert member_record(runner).state is MemberState.PAUSED
        sender = MailboxService(runner.store, runner.scope.team_id, "lead", wake=lambda _: runner.wake(backend.wake))
        sender.send("alice", "待恢复邮件", "paused-mail")
        assert len(provider.requests) == 1
        runner.resume()
        assert runner.wake(backend.wake)
        deadline = time.monotonic()+30
        while len(provider.requests) < 2 and time.monotonic() < deadline:
            time.sleep(.02)
        _wait_idle(backend, runner.scope)
        assert len(provider.requests) == 2 and member_record(runner).state is MemberState.IDLE
        assert not provider.closed
    finally:
        release.set(); backend.shutdown(); runner.close()


def test_member_command_and_git_write_paths_never_start_subprocess(tmp_path, monkeypatch):
    from newcode.tools.command_tool import RunCommandTool
    import newcode.tools.command_tool as commands
    runner, _, provider = setup_member(tmp_path)
    runner.open()
    starts = []
    def forbidden(*args, **kwargs):
        starts.append(args)
        raise AssertionError("不得启动进程")
    monkeypatch.setattr(commands.subprocess, "run", forbidden)
    try:
        assert "run_command" not in runner.registry.names()
        for command in ("git status", "git add README.md", "git commit -m bad", "git update-ref refs/heads/main HEAD", "echo bad"):
            result = RunCommandTool().run({"command": command}, runner.tool_context)
            assert not result.ok
        assert starts == [] and provider.requests == []
    finally:
        runner.close()


def test_oversized_tool_call_exhausts_budget_before_execution(tmp_path):
    def callback(provider, messages, tools):
        yield ToolCallEvent([ToolCall("oversized", "write_file", {"path": "README.md", "content": "x" * 90_000})])
    runner, _, provider = setup_member(tmp_path, callback, permissions=MemberPermissions(mode=PermissionMode.TRUSTED))
    try:
        with pytest.raises(TeamError):
            runner.activate()
        assert len(provider.requests) == 1
        assert (runner.tool_context.cwd / "README.md").read_text() == "base contents\n"
        assert not any(m.role == "tool" for m in runner.session.messages)
        assert member_record(runner).state is MemberState.FAILED
    finally:
        runner.close()


def test_model_read_observation_is_checkpointed_but_not_automatically_acknowledged(tmp_path):
    from newcode.teams.mailbox import MailboxService
    from newcode.tools.types import ToolResult
    runner, _, _ = setup_member(tmp_path)
    try:
        runner.open()
        message = MailboxService(runner.store, runner.scope.team_id, "lead").send("alice", "模型已读", "model-read").messages[0]
        # 可信角色 adapter 的观察端口；本阶段不注册 Team 工具。
        runner.session.add_assistant_tool_call(ToolCall("read-observation", "team_message", {"operation": "read"}))
        runner.session.add_tool_result("read-observation", ToolResult.success("team_message", {"messages": [message.to_dict()]}))
        proof = runner._checkpoint_observations()
        inbox = MailboxService(runner.store, runner.scope.team_id, "alice")
        assert proof.processed_message_ids == (message.message_id,)
        assert [m.message_id for m in inbox.read().messages] == [message.message_id]
        assert inbox.ack(message.message_id, session_id=proof.session_id, checkpoints=runner.checkpoints)
    finally:
        runner.close()


def test_inflight_executor_cannot_be_declared_quiescent_or_taken_over(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def callback(provider, messages, tools):
        yield ToolCallEvent([ToolCall("inflight", "write_file", {"path": "README.md", "content": "admitted result"})])
    runner, kwargs, _ = setup_member(tmp_path, callback, permissions=MemberPermissions(mode=PermissionMode.TRUSTED))
    inner = runner.registry.get("write_file")
    original = inner.run
    def blocked(arguments, context):
        entered.set(); assert release.wait(25)
        return original(arguments, context)
    monkeypatch.setattr(inner, "run", blocked)
    errors = []
    thread = threading.Thread(target=lambda: _capture(errors, runner.activate))
    thread.start()
    try:
        assert entered.wait(25)
        assert not runner.close()
        with pytest.raises(TeamError, match="team_member_busy"):
            with runner.quiescence():
                pytest.fail("在途工具不能获得静止证明")
        candidate = reopened(runner, kwargs)
        with pytest.raises(TeamError, match="team_(member|store)_busy"):
            candidate.open()
        assert not candidate.factory.created
    finally:
        release.set(); thread.join(25); assert runner.close()
    assert errors and not thread.is_alive()
    assert (runner.store.paths.workspace_root / "README.md").read_text() == "base contents\n"


def test_approved_resume_and_revised_plan_recheck_old_response(tmp_path):
    from newcode.teams.types import TeamPlan, TeamTask, TaskState, utc_now
    from newcode.teams.approvals import ApprovalService
    runner, kwargs, provider = setup_member(tmp_path, approval=True,
                                           permissions=MemberPermissions(mode=PermissionMode.TRUSTED))
    try:
        runner.activate("先规划")
        now = utc_now()
        task = TeamTask(new_id("task"), "已分配任务", "", "lead", now, now,
                        assignee="alice", state=TaskState.ASSIGNED)
        plan = TeamPlan(new_id("plan"), "alice", task.task_id, "原计划", now,
                        state=PlanState.APPROVED, decision_member_id="lead", decision_at=now)
        record = member_record(runner)
        team = runner.store.load_team(runner.scope.team_id)
        runner.store.commit_transaction(team.team_id, expected_revision=team.revision, mutation_id=new_id("mutation"),
                                        changes=(RecordChange("tasks", task.task_id, task.to_dict()),
                                                 RecordChange("plans", plan.plan_id, plan.to_dict()),
                                                 RecordChange("members", "alice", replace(record, revision=record.revision+1, plan_revision=1).to_dict())))
        runner.publish_policy(replace(kwargs["policy"], revision=2, plan_state=PlanState.APPROVED, plan_revision=1))
        states = []
        def callback(provider, messages, tools):
            if len(provider.requests) == 2:
                states.append(member_record(runner).state)
                assert "write_file" in {t["function"]["name"] for t in tools}
                ApprovalService(runner.store, runner.scope.team_id, "alice").submit(
                    task.task_id, "修改后的计划", plan_id=plan.plan_id, expected_plan_revision=1)
                yield ToolCallEvent([ToolCall("stale-approval", "write_file", {"path": "README.md", "content": "bad"})])
            else:
                assert "write_file" not in {t["function"]["name"] for t in tools}
                yield TextDelta("重新等待审批")
        provider.callback = callback
        runner.activate("执行获批计划")
        assert states == [MemberState.RUNNING]
        assert (runner.tool_context.cwd / "README.md").read_text() == "base contents\n"
        observation = next(m for m in runner.session.messages if m.tool_call_id == "stale-approval")
        assert json.loads(observation.content)["error"]["code"] == "disallowed_tool"
    finally:
        runner.close()
