from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading

import pytest

from newcode.agent.mode import AgentMode, PLAN_TOOL_NAMES
from newcode.permissions.manager import PermissionManager
from newcode.permissions.rules import PermissionRuleSet
from newcode.permissions.types import (
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMatch,
    PermissionMode,
    PermissionRule,
    RiskLevel,
)
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.session import ChatMessage
from newcode.subagents.manager import SubAgentManager
from newcode.subagents.runner import (
    ChildReadCache,
    DefinitionTask,
    ForkTask,
    SubAgentRunner,
    capture_fork_snapshot,
)
from newcode.subagents.types import (
    AgentDefinition,
    AgentIsolation,
    AgentPermissionMode,
    AgentSource,
    ParentPolicySnapshot,
    TaskState,
    WorkerResult,
)
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolCall, ToolContext, ToolResult, ToolSpec


class FakeProvider:
    def __init__(self, outputs, on_request=None):
        self.outputs = list(outputs)
        self.on_request = on_request
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append({"messages": list(messages), "tools": list(tools or ())})
        if self.on_request:
            self.on_request(len(self.calls), messages, tools)
        yield from self.outputs.pop(0)


class Factory:
    available_models = frozenset({"test-model", "inherited-model"})

    def __init__(self, providers):
        self.providers = list(providers)
        self.created = []

    def create(self, model, *, timeout_seconds):
        self.created.append((model, timeout_seconds))
        return self.providers.pop(0)


@dataclass
class FakeTool:
    name: str
    calls: int = 0
    result: ToolResult | None = None

    @property
    def spec(self):
        properties = {"command": {"type": "string"}} if self.name == "run_command" else {"path": {"type": "string"}}
        required = ["command"] if self.name == "run_command" else ["path"]
        if self.name == "write_file":
            properties["content"] = {"type": "string"}
            required.append("content")
        return ToolSpec(self.name, self.name, {"type": "object", "properties": properties, "required": required})

    def run(self, arguments, context):
        self.calls += 1
        if self.result is not None:
            return self.result
        return ToolResult.success(self.name, {"path": arguments.get("path", "x"), "content": "cached"})


class FakeMCPTool(FakeTool):
    def __init__(self, name="mcp__srv__remote"):
        super().__init__(name)
        self.mcp_metadata = {"mcp_server": "srv", "mcp_tool": "remote", "transport": "stdio"}


def make_definition(*, body="", tools=("read_file",), model=None, permission=AgentPermissionMode.INHERIT, max_iterations=8, isolation=AgentIsolation.SHARED):
    return AgentDefinition(
        name="worker", description="test worker", source=AgentSource.BUILTIN,
        tools_allow=tuple(tools), tools_deny=(), max_iterations=max_iterations,
        permission_mode=permission, model=model, body=body, isolation=isolation,
    )


def setup_runner(tmp_path, provider, registry, *, parent_permission=None, model="test-model", permission_mode=PermissionMode.TRUSTED, tool_context=None):
    holder = {}
    manager = SubAgentManager(lambda context: holder["runner"](context))
    scope = manager.open_session("parent-session")
    policy = ParentPolicySnapshot(scope, frozenset(registry.names()), permission_mode)
    manager.publish_policy_snapshot(scope, policy)
    factory = Factory([provider])
    runner = SubAgentRunner(
        manager=manager,
        provider_factory=factory,
        default_model=model,
        registry=registry,
        tool_context=tool_context or ToolContext(tmp_path, sensitive_values=("secret-value",)),
        parent_permission_manager=parent_permission or PermissionManager(mode=PermissionMode.TRUSTED),
    )
    holder["runner"] = runner
    return manager, scope, factory, runner


def test_definition_runner_has_private_session_injects_sop_each_request_and_cleans_artifacts(tmp_path, monkeypatch):
    read = FakeTool("read_file")
    registry = ToolRegistry()
    registry.register(read)
    parent_session = [ChatMessage("user", "parent-only")]
    provider = FakeProvider([
        [ToolCallEvent([ToolCall("c1", "read_file", {"path": "x.txt"})])],
        [TextDelta("child finished")],
    ])
    from newcode.subagents import runner as runner_module

    captured = {}
    original_loop = runner_module.AgentLoop

    class CapturingLoop(original_loop):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(runner_module, "AgentLoop", CapturingLoop)
    child_root = tmp_path / "child-root"
    child_root.mkdir()
    child_cwd = child_root / "nested"
    child_cwd.mkdir()
    supplied_context = ToolContext(child_root, cwd=child_cwd, sensitive_values=("secret-value",))
    manager, scope, factory, _runner = setup_runner(tmp_path, provider, registry, tool_context=supplied_context)
    try:
        definition = make_definition(body="DEFINITION SOP: do not leak")
        task = manager.start(scope, "do the task", payload=DefinitionTask(definition))
        result = manager.wait(scope, task.task_id, 2).result
        assert result is not None and result.state is TaskState.COMPLETED
        assert result.summary == "child finished"
        assert read.calls == 1
        assert len(provider.calls) == 2
        for call in provider.calls:
            assert any(message.role == "system" and "DEFINITION SOP" in (message.content or "") for message in call["messages"])
            names = [item["function"]["name"] for item in call["tools"]]
            assert "agent" not in names and "load_skill" not in names
        assert parent_session == [ChatMessage("user", "parent-only")]
        artifact_root = child_root / ".newcode" / "context-artifacts"
        assert not list(artifact_root.glob("subagent-*")) if artifact_root.exists() else True
        assert factory.created[0][0] == "test-model"
        assert captured["hook_engine"] is None and captured["memory_service"] is None
        assert captured["skill_state"].activations == ()
        assert "agent" not in captured["registry"].names()
        child_context = captured["tool_context"]
        assert child_context.workspace_root == child_root.resolve()
        assert child_context.cwd == child_cwd.resolve()
        assert child_context.workspace_identity == supplied_context.workspace_identity
    finally:
        manager.shutdown()


def test_worktree_definition_fails_closed_until_phase4_runner_lease_integration(tmp_path):
    registry = ToolRegistry()
    manager, scope, factory, _runner = setup_runner(tmp_path, FakeProvider([]), registry)
    try:
        task = manager.start(
            scope,
            "run isolated task",
            payload=DefinitionTask(make_definition(isolation=AgentIsolation.WORKTREE)),
        )
        result = manager.wait(scope, task.task_id, 2).result
        assert result is not None
        assert result.error_code == "subagent_worktree_unavailable"
        assert factory.created == []
    finally:
        manager.shutdown()


def test_worktree_definition_policy_hides_unverified_run_command(tmp_path):
    from newcode.tools.registry import create_default_registry

    registry = create_default_registry()
    holder = {}
    captured = []

    def worker(task):
        runner = holder["runner"]
        captured.extend(runner._allowed(
            task,
            task.launch_policy,
            frozenset(registry.names()),
            frozenset(),
            AgentMode.DO,
        ))
        return WorkerResult(summary="checked")

    manager = SubAgentManager(worker)
    scope = manager.open_session("parent-session")
    policy = ParentPolicySnapshot(scope, frozenset(registry.names()), PermissionMode.TRUSTED)
    manager.publish_policy_snapshot(scope, policy)
    runner = SubAgentRunner(
        manager=manager,
        provider_factory=Factory([]),
        default_model="test-model",
        registry=registry,
        tool_context=ToolContext(tmp_path),
        parent_permission_manager=PermissionManager(mode=PermissionMode.TRUSTED),
    )
    holder["runner"] = runner
    try:
        task = manager.start(
            scope,
            "check worktree command exposure",
            payload=DefinitionTask(make_definition(
                tools=tuple(registry.names()), isolation=AgentIsolation.WORKTREE,
            )),
        )
        result = manager.wait(scope, task.task_id, 2).result
        assert result is not None and result.summary == "checked"
        assert "run_command" not in captured
        assert {"read_file", "find_files", "search_code"}.issubset(captured)
    finally:
        manager.shutdown()


def test_fork_snapshot_filters_system_and_tool_messages_redacts_and_caps_size():
    source = [
        ChatMessage("system", "dynamic secret-value"),
        ChatMessage("assistant", "tool call", tool_calls=[ToolCall("c", "write_file")]),
        ChatMessage("tool", "tool result", tool_call_id="c"),
        ChatMessage("user", "older"),
        ChatMessage("assistant", "x" * 20_000),
    ]
    snapshot = capture_fork_snapshot(source, ("secret-value",))
    assert len(snapshot) == 1
    assert all(item.role in {"user", "assistant"} and not item.tool_calls for item in snapshot)
    assert sum(len(item.content or "") for item in snapshot) <= 12_000
    assert "secret-value" not in repr(snapshot)
    assert snapshot[-1].content == "x" * 12_000


def test_fork_snapshot_keeps_at_most_twenty_recent_text_messages_and_supports_zero():
    source = [ChatMessage("user", f"message-{index}-" + "x" * 590) for index in range(22)]
    snapshot = capture_fork_snapshot(source)
    assert len(snapshot) <= 20
    assert sum(len(item.content or "") for item in snapshot) <= 12_000
    assert snapshot[-1].content == source[-1].content
    assert capture_fork_snapshot([]) == ()


def test_fork_runner_uses_snapshot_and_explicit_model_without_fallback(tmp_path):
    registry = ToolRegistry()
    provider = FakeProvider([[TextDelta("fork done")]])
    manager, scope, factory, _runner = setup_runner(tmp_path, provider, registry, model="inherited-model")
    try:
        snapshot = (ChatMessage("user", "frozen prompt"), ChatMessage("assistant", "earlier"))
        task = manager.start(scope, "continue", payload=ForkTask(snapshot, model="test-model", allowlist=()))
        outcome = manager.wait(scope, task.task_id, 2)
        assert outcome.result is not None and outcome.result.summary == "fork done"
        assert factory.created[0][0] == "test-model"
        sent = provider.calls[0]["messages"]
        assert any(message.role == "user" and message.content == "frozen prompt" for message in sent)
        assert any(message.role == "assistant" and message.content == "earlier" for message in sent)
        assert all(message.role in {"system", "user", "assistant"} for message in sent)
    finally:
        manager.shutdown()


def test_unsupported_requested_model_fails_without_factory_fallback(tmp_path):
    registry = ToolRegistry()
    provider = FakeProvider([[TextDelta("should not run")]])
    manager, scope, factory, _runner = setup_runner(tmp_path, provider, registry)
    try:
        task = manager.start(scope, "do work", payload=DefinitionTask(make_definition(model="not-supported")))
        result = manager.wait(scope, task.task_id, 2).result
        assert result is not None and result.error_code == "subagent_model_unavailable"
        assert factory.created == []
    finally:
        manager.shutdown()


def test_parent_permission_session_allow_is_not_inherited_and_confirmation_denies_before_executor(tmp_path):
    write = FakeTool("write_file")
    registry = ToolRegistry()
    registry.register(write, read_only=False, do_visible=True)
    session_allow = PermissionRuleSet(
        source=PermissionLayer.SESSION_RULES,
        rules=(PermissionRule(
            id="parent-session-allow", tool="write_file",
            match=PermissionMatch(path="x.txt"), action=PermissionDecisionValue.ALLOW,
            reason="parent-only", risk_level=RiskLevel.MEDIUM, source=PermissionLayer.SESSION_RULES,
        ),),
    )
    parent_permission = PermissionManager(mode=PermissionMode.TRUSTED, session_rules=session_allow)
    provider = FakeProvider([
        [ToolCallEvent([ToolCall("c1", "write_file", {"path": "x.txt", "content": "no"})])],
        [TextDelta("safe refusal")],
    ])
    manager, scope, _factory, _runner = setup_runner(
        tmp_path, provider, registry, parent_permission=parent_permission, permission_mode=PermissionMode.DEFAULT,
    )
    try:
        definition = make_definition(tools=("write_file",))
        task = manager.start(scope, "try writing", payload=DefinitionTask(definition))
        result = manager.wait(scope, task.task_id, 2).result
        assert result is not None and result.state is TaskState.COMPLETED
        assert write.calls == 0
    finally:
        manager.shutdown()


@pytest.mark.parametrize("shrink_kind", ["do_to_plan", "skill_whitelist"])
def test_policy_narrowing_after_request_blocks_executor_and_next_request_hides_tool(tmp_path, shrink_kind):
    write = FakeTool("write_file")
    read = FakeTool("read_file")
    registry = ToolRegistry()
    registry.register(read)
    registry.register(write, read_only=False, do_visible=True)
    request_started = threading.Event()
    release_request = threading.Event()

    def narrow_after_schema(call_number, _messages, _tools):
        if call_number == 1:
            request_started.set()
            release_request.wait(2)

    provider = FakeProvider([
        [ToolCallEvent([ToolCall("c1", "write_file", {"path": "x.txt", "content": "no"})])],
        [TextDelta("policy kept")],
    ], on_request=narrow_after_schema)
    manager, scope, _factory, runner = setup_runner(tmp_path, provider, registry)
    try:
        definition = make_definition(tools=("read_file", "write_file"))
        task = manager.start(scope, "try", payload=DefinitionTask(definition))
        assert request_started.wait(1)
        allowed = {"read_file"} if shrink_kind == "skill_whitelist" else ({"read_file", "write_file"} & PLAN_TOOL_NAMES)
        # 此处是测试主线程模拟父 CLI 收窄并发布不可变策略快照。
        manager.publish_policy_snapshot(scope, ParentPolicySnapshot(scope, frozenset(allowed), PermissionMode.TRUSTED))
        release_request.set()
        outcome = manager.wait(scope, task.task_id, 2)
        assert outcome.result is not None and outcome.result.summary == "policy kept"
        assert write.calls == 0
        assert all("write_file" not in [item["function"]["name"] for item in call["tools"]] for call in provider.calls[1:])
    finally:
        release_request.set()
        manager.shutdown()


def test_parent_permission_mode_tightening_is_applied_before_executor(tmp_path):
    write = FakeTool("write_file")
    registry = ToolRegistry()
    registry.register(write, read_only=False, do_visible=True)
    request_started = threading.Event()
    release_request = threading.Event()

    def tighten(call_number, _messages, _tools):
        if call_number == 1:
            request_started.set()
            release_request.wait(2)

    provider = FakeProvider([
        [ToolCallEvent([ToolCall("c1", "write_file", {"path": "x.txt", "content": "no"})])],
        [TextDelta("confirmation denied")],
    ], on_request=tighten)
    manager, scope, _factory, _runner = setup_runner(tmp_path, provider, registry)
    try:
        task = manager.start(scope, "write", payload=DefinitionTask(make_definition(tools=("write_file",))))
        assert request_started.wait(1)
        manager.publish_policy_snapshot(scope, ParentPolicySnapshot(scope, frozenset({"write_file"}), PermissionMode.STRICT))
        release_request.set()
        result = manager.wait(scope, task.task_id, 2).result
        assert result is not None and result.summary == "confirmation denied"
        assert write.calls == 0
    finally:
        release_request.set()
        manager.shutdown()


def test_parent_permission_deny_rule_published_during_child_run_blocks_tool(tmp_path):
    read = FakeTool("read_file")
    registry = ToolRegistry()
    registry.register(read)
    entered, release = threading.Event(), threading.Event()

    def pause_provider(call_number, _messages, _tools):
        if call_number == 1:
            entered.set()
            release.wait(2)

    provider = FakeProvider([
        [ToolCallEvent([ToolCall("r1", "read_file", {"path": "private.txt"})])],
        [TextDelta("blocked by live parent deny")],
    ], on_request=pause_provider)
    manager, scope, _factory, _runner = setup_runner(tmp_path, provider, registry)
    deny = PermissionRule(
        id="live-parent-deny", tool="read_file", match=PermissionMatch(path="private.txt"),
        action=PermissionDecisionValue.DENY, reason="parent denied", risk_level=RiskLevel.LOW,
        source=PermissionLayer.SESSION_RULES,
    )
    try:
        task = manager.start(scope, "read private file", payload=DefinitionTask(make_definition()))
        assert entered.wait(1)
        manager.publish_policy_snapshot(
            scope,
            ParentPolicySnapshot(
                scope, frozenset(registry.names()), PermissionMode.TRUSTED,
                permission_deny_rules=(deny,),
            ),
        )
        release.set()
        outcome = manager.wait(scope, task.task_id, 2)
        assert outcome.result is not None and outcome.result.summary == "blocked by live parent deny"
        assert read.calls == 0
    finally:
        release.set()
        manager.shutdown()


def test_cache_bounds_stat_invalidation_and_child_cache_hit_still_reaches_permission_check(tmp_path, monkeypatch):
    path = tmp_path / "cache.txt"
    path.write_text("first", encoding="utf-8")
    cache = ChildReadCache(max_items=1, max_bytes=1024)
    cached = ToolResult.success("read_file", {"content": "first"})
    cache.put(path, cached)
    assert cache.get(path) == cached
    path.write_text("changed with different size", encoding="utf-8")
    assert cache.get(path) is None
    cache.put(path, cached)
    another = tmp_path / "another.txt"
    another.write_text("x", encoding="utf-8")
    cache.put(another, cached)
    assert cache.size <= 1 and cache.byte_size <= 1024

    read = FakeTool("read_file")
    registry = ToolRegistry()
    registry.register(read)
    provider = FakeProvider([
        [ToolCallEvent([ToolCall("c1", "read_file", {"path": "cache.txt"})])],
        [ToolCallEvent([ToolCall("c2", "read_file", {"path": "cache.txt"})])],
        [TextDelta("done")],
    ])
    manager, scope, _factory, _runner = setup_runner(tmp_path, provider, registry)
    calls = 0
    original_check = PermissionManager.check

    def counted_check(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_check(self, *args, **kwargs)

    monkeypatch.setattr(PermissionManager, "check", counted_check)
    try:
        task = manager.start(scope, "read twice", payload=DefinitionTask(make_definition()))
        outcome = manager.wait(scope, task.task_id, 2)
        assert outcome.result is not None and outcome.result.summary == "done"
        assert read.calls == 1
        assert calls >= 4  # 两次 AgentLoop precheck + 两次 cache/executor guard 检查。
    finally:
        manager.shutdown()


def test_child_read_cache_identity_separates_worktree_roots(tmp_path):
    from newcode.subagents.runner import ChildReadCache

    main = tmp_path / "main"
    child = tmp_path / "child"
    main.mkdir()
    child.mkdir()
    main_file = main / "same.txt"
    child_file = child / "same.txt"
    main_file.write_text("main", encoding="utf-8")
    child_file.write_text("child", encoding="utf-8")
    main_context = ToolContext(main)
    child_context = ToolContext(child, worktree_task_id="task-1234")
    main_cache = ChildReadCache(workspace_root=main, workspace_identity=main_context.workspace_identity)
    child_cache = ChildReadCache(workspace_root=child, workspace_identity=child_context.workspace_identity)
    main_result = ToolResult.success("read_file", {"content": "main"})
    child_result = ToolResult.success("read_file", {"content": "child"})

    main_cache.put(main_file, main_result)
    child_cache.put(child_file, child_result)

    assert main_context.workspace_identity != child_context.workspace_identity
    assert main_cache.get(main_file) == main_result
    assert main_cache.get(child_file) is None
    assert child_cache.get(child_file) == child_result
    assert child_cache.get(main_file) is None


@pytest.mark.parametrize("action", ["write_file", "run_command"])
def test_child_read_cache_is_invalidated_after_write_or_command_execution(tmp_path, action):
    read = FakeTool("read_file")
    write = FakeTool(action)
    registry = ToolRegistry()
    registry.register(read)
    registry.register(write, read_only=False, do_visible=True)
    (tmp_path / "cache.txt").write_text("x", encoding="utf-8")
    provider = FakeProvider([
        [ToolCallEvent([ToolCall("c1", "read_file", {"path": "cache.txt"})])],
        [ToolCallEvent([ToolCall("c2", action, {"command": "echo safe"} if action == "run_command" else {"path": "cache.txt", "content": "changed"})])],
        [ToolCallEvent([ToolCall("c3", "read_file", {"path": "cache.txt"})])],
        [TextDelta("done")],
    ])
    manager, scope, _factory, _runner = setup_runner(tmp_path, provider, registry)
    try:
        task = manager.start(
            scope,
            "read and write",
            payload=DefinitionTask(make_definition(tools=("read_file", action))),
        )
        result = manager.wait(scope, task.task_id, 2).result
        assert result is not None and result.summary == "done"
        assert read.calls == 2
        assert write.calls == 1
    finally:
        manager.shutdown()


def test_mcp_tool_still_requires_permission_and_never_bypasses_existing_gate(tmp_path):
    mcp = FakeMCPTool()
    registry = ToolRegistry()
    registry.register(mcp, read_only=False, do_visible=True)
    provider = FakeProvider([
        [ToolCallEvent([ToolCall("m1", mcp.spec.name, {})])],
        [TextDelta("denied safely")],
    ])
    manager, scope, _factory, _runner = setup_runner(tmp_path, provider, registry)
    try:
        task = manager.start(scope, "call mcp", payload=DefinitionTask(make_definition(tools=(mcp.spec.name,))))
        outcome = manager.wait(scope, task.task_id, 2)
        assert outcome.result is not None and outcome.result.summary == "denied safely"
        assert mcp.calls == 0
    finally:
        manager.shutdown()


def test_child_file_tool_outside_workspace_is_denied_by_existing_sandbox(tmp_path):
    read = FakeTool("read_file")
    registry = ToolRegistry()
    registry.register(read)
    provider = FakeProvider([
        [ToolCallEvent([ToolCall("r1", "read_file", {"path": str(tmp_path.parent / "outside.txt")})])],
        [TextDelta("sandbox denied")],
    ])
    manager, scope, _factory, _runner = setup_runner(tmp_path, provider, registry)
    try:
        task = manager.start(scope, "try outside", payload=DefinitionTask(make_definition()))
        result = manager.wait(scope, task.task_id, 2).result
        assert result is not None and result.summary == "sandbox denied"
        assert read.calls == 0
    finally:
        manager.shutdown()


def test_allowed_fake_mcp_execution_invalidates_child_read_cache(tmp_path):
    read = FakeTool("read_file")
    mcp = FakeMCPTool()
    registry = ToolRegistry()
    registry.register(read)
    registry.register(mcp, read_only=False, do_visible=True)
    (tmp_path / "cache.txt").write_text("x", encoding="utf-8")
    allow_rule = PermissionRule(
        id="allow-local-fake-mcp", tool=mcp.spec.name,
        match=PermissionMatch(mcp_server="srv", mcp_tool="remote"),
        action=PermissionDecisionValue.ALLOW, reason="test fixture only",
        risk_level=RiskLevel.MEDIUM, source=PermissionLayer.PROJECT_RULES,
    )
    permission = PermissionManager(
        mode=PermissionMode.TRUSTED,
        project_rules=PermissionRuleSet(PermissionLayer.PROJECT_RULES, (allow_rule,)),
    )
    provider = FakeProvider([
        [ToolCallEvent([ToolCall("r1", "read_file", {"path": "cache.txt"})])],
        [ToolCallEvent([ToolCall("m1", mcp.spec.name, {})])],
        [ToolCallEvent([ToolCall("r2", "read_file", {"path": "cache.txt"})])],
        [TextDelta("done")],
    ])
    manager, scope, _factory, _runner = setup_runner(tmp_path, provider, registry, parent_permission=permission)
    try:
        definition = make_definition(tools=("read_file", mcp.spec.name))
        task = manager.start(scope, "read and use local fixture", payload=DefinitionTask(definition))
        result = manager.wait(scope, task.task_id, 2).result
        assert result is not None and result.summary == "done"
        assert mcp.calls == 1 and read.calls == 2
    finally:
        manager.shutdown()


def test_definition_runner_removes_artifacts_when_provider_fails(tmp_path):
    class ExplodingProvider:
        def stream_chat(self, *_args, **_kwargs):
            raise RuntimeError("secret path /token")
            yield

    registry = ToolRegistry()
    manager, scope, _factory, _runner = setup_runner(tmp_path, ExplodingProvider(), registry)
    try:
        task = manager.start(scope, "fail", payload=DefinitionTask(make_definition()))
        result = manager.wait(scope, task.task_id, 2).result
        assert result is not None and result.error_code == "subagent_provider_error"
        assert not (tmp_path / ".newcode" / "context-artifacts" / f"subagent-{task.task_id[:48]}").exists()
    finally:
        manager.shutdown()
