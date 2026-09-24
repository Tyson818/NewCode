from __future__ import annotations

from threading import Event
from pathlib import Path

from newcode.permissions.types import PermissionMode
from newcode.subagents.manager import SubAgentManager
from newcode.subagents.tool import AgentTool
from newcode.subagents.types import (
    AgentCatalog,
    AgentDefinition,
    AgentPermissionMode,
    AgentSource,
    ParentPolicySnapshot,
    SessionScope,
    TaskState,
    WorkerResult,
)
from newcode.agent.mode import AgentMode
from newcode.tools.types import ToolContext
from newcode.hooks.actions import HookActionRunner
from newcode.hooks.types import HookAction, HookActionType, HookContext, HookEvent, HookRule, HookSource


def definition(name="worker"):
    return AgentDefinition(
        name=name, description="safe summary", source=AgentSource.BUILTIN,
        tools_allow=(), tools_deny=(), max_iterations=2,
        permission_mode=AgentPermissionMode.INHERIT,
    )


def make_tool(manager, scope, catalog=None, snapshot_provider=lambda: (), mode=AgentMode.DO):
    return AgentTool(
        manager=manager,
        scope=scope,
        catalog=catalog or AgentCatalog(()),
        snapshot_provider=snapshot_provider,
        mode_provider=lambda: mode,
    )


def test_agent_tool_has_one_fixed_schema_with_all_operations_and_no_dynamic_fields():
    manager = SubAgentManager(lambda _: WorkerResult())
    scope = manager.open_session("schema")
    try:
        tool = make_tool(manager, scope, AgentCatalog((definition(),)))
        spec = tool.spec
        assert spec.name == "agent"
        assert spec.parameters["required"] == ["operation"]
        assert set(spec.parameters["properties"]["operation"]["enum"]) == {
            "start", "status", "wait", "background", "cancel", "collect",
        }
        assert spec.parameters["additionalProperties"] is False
        assert "wait_seconds" in spec.parameters["properties"]
        assert "operation" not in spec.parameters["required"] or spec.parameters["required"] == ["operation"]
    finally:
        manager.shutdown()


def test_start_kind_combinations_and_invalid_fields_fail_before_task_creation():
    manager = SubAgentManager(lambda _: WorkerResult())
    scope = manager.open_session("tool-invalid")
    tool = make_tool(manager, scope, AgentCatalog((definition(),)))
    context = ToolContext(Path.cwd())
    invalid = [
        {},
        {"operation": "start"},
        {"operation": "start", "kind": "definition", "agent_name": "worker", "task_prompt": "x", "execution": "foreground", "allowlist": []},
        {"operation": "start", "kind": "fork", "agent_name": "worker", "task_prompt": "x", "execution": "foreground"},
        {"operation": "start", "kind": "fork", "task_prompt": "x", "execution": "foreground", "allowlist": "read_file"},
        {"operation": "start", "kind": "fork", "task_prompt": "x", "execution": "foreground", "extra": True},
        {"operation": "wait", "task_id": "id", "wait_seconds": True},
        {"operation": "status", "task_id": "id", "wait_seconds": 1},
        {"operation": "nonsense"},
    ]
    try:
        for args in invalid:
            result = tool.run(args, context)
            assert not result.ok and result.error.code == "subagent_invalid_request"
        assert not manager._tasks
    finally:
        manager.shutdown()


def test_definition_and_fork_start_return_task_id_and_operations_claim_once():
    entered, release = Event(), Event()

    def worker(task):
        if task.task_input == "blocked":
            entered.set()
            release.wait(1)
        return WorkerResult(summary="secret-token-result")

    manager = SubAgentManager(worker)
    scope = manager.open_session("tool-ops")
    manager.publish_policy_snapshot(scope, ParentPolicySnapshot(
        scope, frozenset({"read_file"}), PermissionMode.TRUSTED,
    ))
    tool = make_tool(manager, scope, AgentCatalog((definition(),)), lambda: ())
    context = ToolContext(Path.cwd(), sensitive_values=("secret-token",))
    try:
        started = tool.run({
            "operation": "start", "kind": "definition", "agent_name": "worker",
            "task_prompt": "blocked", "execution": "background",
        }, context)
        assert started.ok
        task_id = started.data["task_id"]
        assert entered.wait(1)
        assert tool.run({"operation": "status", "task_id": task_id}, context).ok
        assert tool.run({"operation": "background", "task_id": task_id}, context).ok
        release.set()
        wait = tool.run({"operation": "wait", "task_id": task_id, "wait_seconds": 1}, context)
        assert wait.ok and wait.data["result"]["summary"] == "[REDACTED]-result"
        repeated = tool.run({"operation": "collect", "task_id": task_id}, context)
        assert not repeated.ok and repeated.error.code == "subagent_result_already_collected"
        assert tool.run({"operation": "cancel", "task_id": task_id}, context).ok

        fork = tool.run({
            "operation": "start", "kind": "fork", "task_prompt": "fork",
            "execution": "foreground", "allowlist": [], "model_override": "supported-by-runner",
        }, context)
        assert fork.ok and fork.data["state"] in {TaskState.QUEUED.value, TaskState.RUNNING.value}
    finally:
        release.set()
        manager.shutdown()


def test_hook_subagent_action_remains_unavailable_and_does_not_enqueue_manager_task():
    manager = SubAgentManager(lambda _: WorkerResult(summary="must not run"))
    scope = manager.open_session("hook-placeholder")
    runner = HookActionRunner()
    rule = HookRule(
        id="placeholder", event=HookEvent.TURN_START,
        action=HookAction(HookActionType.SUBAGENT, {}), source=HookSource.USER,
    )
    try:
        assert runner.submit(rule, HookContext(HookEvent.TURN_START), 0) is False
        assert runner.diagnostics[0].code == "hook_subagent_not_available"
        assert manager._tasks == {}
    finally:
        runner.shutdown()
        manager.shutdown()
