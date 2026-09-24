from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time

from newcode.agent import AgentFinalAnswer, AgentLoop
from newcode.agent.mode import AgentMode
from newcode.context.manager import ContextManager
from newcode.permissions.confirmer import DenyByDefaultConfirmer
from newcode.permissions.manager import PermissionManager
from newcode.permissions.types import (
    PermissionDecision,
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMode,
    RiskLevel,
)
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.skills.state import ActiveSkillState
from newcode.skills.types import LoadedSkill, SkillCatalog, SkillFrontmatter, SkillMetadata, SkillMode, SkillSource
from newcode.subagents.manager import SubAgentManager
from newcode.subagents.types import (
    AgentCatalog,
    AgentDefinition,
    AgentDirectoryEntry,
    AgentPermissionMode,
    AgentSource,
    ParentPolicySnapshot,
    TaskExecution,
    WorkerResult,
)
from newcode.tools.registry import ToolRegistry, create_default_registry
from newcode.tools.types import ToolCall, ToolContext


class Provider:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append({"messages": list(messages), "tools": list(tools or ()), "allow": allow_tool_calls})
        yield from self.outputs.pop(0)


def agent_call(call_id="agent-1", **arguments):
    return ToolCall(call_id, "agent", arguments)


def definition():
    return AgentDefinition(
        name="reviewer", description="Reviews local changes", source=AgentSource.BUILTIN,
        tools_allow=("read_file",), tools_deny=(), max_iterations=3,
        permission_mode=AgentPermissionMode.INHERIT, body="PRIVATE-SOP-BODY",
        digest="PRIVATE-DIGEST", model="PRIVATE-MODEL", root=Path("PRIVATE-PATH"), entry=Path("PRIVATE-ENTRY"),
    )


def make_loop(tmp_path, provider, worker, *, mode=AgentMode.DO, permission=None, session=None, context_manager=None, skill_state=None, skill_catalog=None, agent_catalog=None):
    manager = SubAgentManager(worker)
    current_session = session or ChatSession(session_id="parent-loop")
    scope = manager.open_session(current_session.session_id)
    loop = AgentLoop(
        provider=provider,
        session=current_session,
        registry=create_default_registry(),
        tool_context=ToolContext(tmp_path, sensitive_values=("private-secret",)),
        permission_manager=permission or PermissionManager(mode=PermissionMode.TRUSTED),
        context_manager=context_manager,
        skill_state=skill_state,
        skill_catalog=skill_catalog,
        subagent_manager=manager,
        subagent_scope=scope,
        agent_catalog=agent_catalog or AgentCatalog((definition(),)),
    )
    return loop, manager, scope


def test_agent_tool_visible_in_plan_and_do_and_scheduler_keeps_it_serial(tmp_path):
    started = []
    worker = lambda task: (started.append(task), WorkerResult(summary="done"))[1]
    provider = Provider([
        [ToolCallEvent([agent_call(
            operation="start", kind="definition", agent_name="reviewer",
            task_prompt="plan child", execution="foreground",
        )])],
        [TextDelta("plan done")],
        [ToolCallEvent([agent_call(
            "agent-2", operation="start", kind="definition", agent_name="reviewer",
            task_prompt="do child", execution="foreground",
        )])],
        [TextDelta("do done")],
    ])
    loop, manager, scope = make_loop(tmp_path, provider, worker)
    try:
        assert any(isinstance(event, AgentFinalAnswer) for event in loop.run("plan", mode=AgentMode.PLAN))
        assert any(isinstance(event, AgentFinalAnswer) for event in loop.run("do", mode=AgentMode.DO))
        for call in provider.calls:
            names = {item["function"]["name"] for item in call["tools"]}
            assert "agent" in names
        assert loop.registry.is_read_only("agent") is False
        assert all(not batch.parallel for batch in loop.scheduler.make_batches([agent_call(operation="status", task_id="x")]))
        assert len(started) == 2
        assert manager.policy_snapshot(scope).visible_tools <= set(loop.registry.names()) - {"agent", "load_skill"}
    finally:
        manager.shutdown()


def test_permission_confirmation_and_skill_visibility_reject_before_task_creation(tmp_path):
    provider = Provider([
        [ToolCallEvent([agent_call(
            operation="start", kind="definition", agent_name="reviewer",
            task_prompt="must not start", execution="background",
        )])],
        [TextDelta("permission denied")],
    ])
    loop, manager, _scope = make_loop(
        tmp_path,
        provider,
        lambda _task: WorkerResult(summary="should not execute"),
        permission=PermissionManager(mode=PermissionMode.DEFAULT, confirmer=DenyByDefaultConfirmer()),
    )
    try:
        list(loop.run("request", mode=AgentMode.DO))
        assert manager._tasks == {}
    finally:
        manager.shutdown()

    provider = Provider([
        [ToolCallEvent([agent_call(
            operation="start", kind="definition", agent_name="reviewer",
            task_prompt="explicit deny", execution="background",
        )])],
        [TextDelta("permission denied safely")],
    ])
    permission = PermissionManager(mode=PermissionMode.TRUSTED)
    permission.check = lambda *_args, **_kwargs: PermissionDecision(
        decision=PermissionDecisionValue.DENY,
        tool_name="agent",
        reason="test deny",
        risk_level=RiskLevel.HIGH,
        matched_rule="test-deny-agent",
        layer=PermissionLayer.SESSION_RULES,
    )
    loop, manager, _scope = make_loop(
        tmp_path, provider, lambda _task: WorkerResult(), permission=permission,
    )
    try:
        list(loop.run("explicit deny", mode=AgentMode.DO))
        assert manager._tasks == {}
    finally:
        manager.shutdown()

    state = ActiveSkillState()
    metadata = SkillMetadata(
        frontmatter=SkillFrontmatter("restricted", "no agent", ("read_file",), SkillMode.SHARED),
        source=SkillSource.BUILTIN, root=tmp_path, entry=tmp_path / "SKILL.md", digest="x",
    )
    state.activate(LoadedSkill(metadata, "safe SOP", ()))
    provider = Provider([[ToolCallEvent([agent_call(
        operation="start", kind="definition", agent_name="reviewer",
        task_prompt="skill denied", execution="foreground",
    )])]])
    loop, manager, _scope = make_loop(
        tmp_path, provider, lambda _task: WorkerResult(), skill_state=state,
        skill_catalog=SkillCatalog((metadata,)),
    )
    try:
        list(loop.run("request", mode=AgentMode.DO))
        assert manager._tasks == {}
    finally:
        manager.shutdown()


def test_agent_catalog_prompt_contains_only_name_and_description_and_is_not_persisted(tmp_path):
    provider = Provider([[TextDelta("done")]])
    secret_description = AgentDefinition(
        name="reviewer", description="Reads token=private-secret from C:\\Users\\Admin\\project",
        source=AgentSource.PROJECT, tools_allow=("read_file",), tools_deny=(), max_iterations=2,
        permission_mode=AgentPermissionMode.INHERIT, body="PRIVATE-SOP-BODY",
        digest="PRIVATE-DIGEST", model="PRIVATE-MODEL", root=Path("PRIVATE-PATH"),
    )
    loop, manager, _scope = make_loop(
        tmp_path, provider, lambda _task: WorkerResult(), agent_catalog=AgentCatalog((secret_description,)),
    )
    try:
        list(loop.run("catalog", mode=AgentMode.DO))
        systems = [message.content or "" for message in provider.calls[0]["messages"] if message.role == "system"]
        catalog_blocks = [item for item in systems if "reviewer" in item]
        assert len(catalog_blocks) == 1
        assert "Reads" in catalog_blocks[0] and "[REDACTED]" in catalog_blocks[0]
        for secret in ("PRIVATE-SOP-BODY", "PRIVATE-DIGEST", "PRIVATE-MODEL", "PRIVATE-PATH", "read_file", "private-secret", "C:\\Users"):
            assert secret not in catalog_blocks[0]
        assert all(message.role != "system" for message in loop.session.messages)
        assert all("reviewer" not in (message.content or "") for message in loop.session.messages)
    finally:
        manager.shutdown()


class SpyContextManager(ContextManager):
    def __init__(self, session, workspace):
        super().__init__(session, workspace, sensitive_values=("private-secret",))
        self.seen_before_prepare = None
        self.estimated_after_notice = None

    def prepare(self, generator=None):
        self.seen_before_prepare = tuple(self.session.messages)
        self.estimator.invalidate_for_version(self.session.context_version)
        self.estimated_after_notice = self.estimator.estimate(self.session.messages, self.session.context_version)
        return super().prepare(generator)


def _wait_complete(manager, scope, task_id):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if manager.status(scope, task_id).result_available:
            return
        time.sleep(0.005)
    raise AssertionError("task did not complete")


def test_background_notifications_deliver_on_main_safe_point_before_context_prepare_once(tmp_path, monkeypatch):
    worker_threads = []
    release = {"one": threading.Event(), "two": threading.Event()}

    def worker(task):
        worker_threads.append(threading.get_ident())
        release[task.task_input].wait(1)
        return WorkerResult(summary=f"{task.task_input}-private-secret" + "x" * 5000)

    provider = Provider([[TextDelta("parent done")], [TextDelta("next done")]])
    session = ChatSession(session_id="notice-session")
    context = SpyContextManager(session, tmp_path)
    loop, manager, scope = make_loop(tmp_path, provider, worker, session=session, context_manager=context)
    task1 = manager.start(scope, "one", execution=TaskExecution.BACKGROUND)
    task2 = manager.start(scope, "two", execution=TaskExecution.BACKGROUND)
    assert task1.task_id != task2.task_id
    thread_ids = []
    original_add = ChatSession.add_assistant_message

    def tracked_add(self, content):
        thread_ids.append(threading.get_ident())
        return original_add(self, content)

    monkeypatch.setattr(ChatSession, "add_assistant_message", tracked_add)
    release["two"].set()
    _wait_complete(manager, scope, task2.task_id)
    release["one"].set()
    _wait_complete(manager, scope, task1.task_id)
    main_thread = threading.get_ident()
    try:
        list(loop.run("parent turn", mode=AgentMode.DO))
        notes = [message.content or "" for message in session.messages if "【子 Agent 后台任务通知】" in (message.content or "")]
        assert len(notes) == 2
        assert task2.task_id in notes[0] and task1.task_id in notes[1]
        assert notes[0].find("摘要：") < notes[0].find("[REDACTED]")
        assert all(len(note) <= 4000 and "private-secret" not in note for note in notes)
        assert context.seen_before_prepare is not None
        assert sum("【子 Agent 后台任务通知】" in (message.content or "") for message in context.seen_before_prepare) == 2
        assert context.estimated_after_notice > 0
        assert set(thread_ids) == {main_thread}
        assert all(worker_thread != main_thread for worker_thread in worker_threads)

        before = len(notes)
        list(loop.run("second turn", mode=AgentMode.DO))
        after = sum("【子 Agent 后台任务通知】" in (message.content or "") for message in session.messages)
        assert after == before
    finally:
        for item in release.values():
            item.set()
        manager.shutdown()


def test_parent_policy_snapshots_shrink_do_to_plan_and_skill_whitelist(tmp_path):
    provider = Provider([[TextDelta("do")], [TextDelta("plan")]])
    loop, manager, scope = make_loop(tmp_path, provider, lambda _task: WorkerResult())
    try:
        list(loop.run("do", mode=AgentMode.DO))
        do_snapshot = manager.policy_snapshot(scope)
        assert "write_file" in do_snapshot.visible_tools
        list(loop.run("plan", mode=AgentMode.PLAN))
        plan_snapshot = manager.policy_snapshot(scope)
        assert "write_file" not in plan_snapshot.visible_tools
        assert plan_snapshot.visible_tools <= do_snapshot.visible_tools
    finally:
        manager.shutdown()


def test_agent_loop_publishes_skill_whitelist_shrink_for_running_children(tmp_path):
    provider = Provider([[TextDelta("before skill")], [TextDelta("after skill")]])
    state = ActiveSkillState()
    skill_metadata = SkillMetadata(
        frontmatter=SkillFrontmatter("reader", "read only", ("read_file",), SkillMode.SHARED),
        source=SkillSource.BUILTIN, root=tmp_path, entry=tmp_path / "SKILL.md", digest="reader-v1",
    )
    skill_state_catalog = SkillCatalog((skill_metadata,))
    # 构造后固定 discovery catalog，以便用 state.activate 模拟当前会话 activation 收窄。
    manager = SubAgentManager(lambda _task: WorkerResult())
    session = ChatSession(session_id="skill-policy")
    scope = manager.open_session(session.session_id)
    loop = AgentLoop(
        provider=provider,
        session=session,
        registry=create_default_registry(),
        tool_context=ToolContext(tmp_path),
        permission_manager=PermissionManager(mode=PermissionMode.TRUSTED),
        skill_state=state,
        skill_catalog=skill_state_catalog,
        subagent_manager=manager,
        subagent_scope=scope,
        agent_catalog=AgentCatalog((definition(),)),
    )
    try:
        list(loop.run("unrestricted", mode=AgentMode.DO))
        before = manager.policy_snapshot(scope)
        assert "write_file" in before.visible_tools
        state.activate(LoadedSkill(skill_metadata, "SOP", ()))
        list(loop.run("skill restricted", mode=AgentMode.DO))
        after = manager.policy_snapshot(scope)
        assert after.visible_tools == {"read_file"}
        assert after.visible_tools < before.visible_tools
    finally:
        manager.shutdown()


def test_collect_wins_over_notice_and_new_generation_cannot_receive_old_session_result(tmp_path):
    completed = threading.Event()

    def worker(task):
        completed.set()
        return WorkerResult(summary="one-time result")

    provider = Provider([[TextDelta("no duplicate")]])
    session = ChatSession(session_id="generation-bound")
    loop, manager, old_scope = make_loop(tmp_path, provider, worker, session=session)
    task = manager.start(old_scope, "old result", execution=TaskExecution.BACKGROUND)
    try:
        assert completed.wait(1)
        deadline = time.monotonic() + 1
        while not manager.status(old_scope, task.task_id).result_available and time.monotonic() < deadline:
            time.sleep(0.005)
        claimed = manager.collect(old_scope, task.task_id)
        assert claimed.summary == "one-time result"
        list(loop.run("after collect", mode=AgentMode.DO))
        assert not any("子 Agent 后台任务通知" in (message.content or "") for message in session.messages)

        old2 = manager.open_session("generation-bound")
        stale_task = manager.start(old2, "stale", execution=TaskExecution.BACKGROUND)
        deadline = time.monotonic() + 1
        while not manager.status(old2, stale_task.task_id).result_available and time.monotonic() < deadline:
            time.sleep(0.005)
        manager.close_session(old2)
        new_scope = manager.open_session("generation-bound")
        new_loop = AgentLoop(
            provider=Provider([[TextDelta("new session")]]),
            session=session,
            registry=create_default_registry(),
            tool_context=ToolContext(tmp_path),
            permission_manager=PermissionManager(mode=PermissionMode.TRUSTED),
            subagent_manager=manager,
            subagent_scope=new_scope,
            agent_catalog=AgentCatalog(()),
        )
        list(new_loop.run("new generation", mode=AgentMode.DO))
        assert not any("stale" in (message.content or "") for message in session.messages)
    finally:
        manager.shutdown()


def test_notification_and_collect_race_claims_result_exactly_once(tmp_path):
    completed = threading.Event()

    def worker(_task):
        completed.set()
        return WorkerResult(summary="single delivery")

    loop, manager, scope = make_loop(
        tmp_path, Provider([[TextDelta("done")]]), worker,
        session=ChatSession(session_id="claim-race"),
    )
    task = manager.start(scope, "race", execution=TaskExecution.BACKGROUND)
    collect_result = []
    collect_error = []
    barrier = threading.Barrier(2)

    def collect_at_barrier():
        barrier.wait()
        try:
            collect_result.append(manager.collect(scope, task.task_id))
        except Exception as exc:  # The competing safe-point claim may win atomically.
            collect_error.append(getattr(exc, "code", type(exc).__name__))

    collector = threading.Thread(target=collect_at_barrier)
    collector.start()
    try:
        assert completed.wait(1)
        _wait_complete(manager, scope, task.task_id)
        barrier.wait()
        collector.join(timeout=1)
        assert not collector.is_alive()

        # This is the same main-thread safe-point drain used before Context preparation.
        loop._deliver_subagent_notifications()
        notice_count = sum(
            "【子 Agent 后台任务通知】" in (message.content or "")
            for message in loop.session.messages
        )
        assert (len(collect_result), notice_count) in {(1, 0), (0, 1)}
        list(loop.run("after race", mode=AgentMode.DO))
        assert sum(
            "【子 Agent 后台任务通知】" in (message.content or "")
            for message in loop.session.messages
        ) == notice_count
    finally:
        collector.join(timeout=1)
        manager.shutdown()
