from __future__ import annotations

import threading

import pytest

from newcode.agent.mode import AgentMode, PLAN_TOOL_NAMES
from newcode.permissions.types import PermissionMode
from newcode.subagents.manager import SubAgentManager
from newcode.subagents.policy import child_tool_names
from newcode.subagents.types import ParentPolicySnapshot, SessionScope, SubAgentManagerError, TaskExecution, WorkerResult
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolResult, ToolSpec


class Tool:
    def __init__(self, name):
        self.spec = ToolSpec(name, name, {"type": "object", "properties": {}})

    def run(self, arguments, context):
        return ToolResult.success(self.spec.name)


def registry():
    result = ToolRegistry()
    for name in ("read_file", "write_file", "find_files", "search_code", "agent", "load_skill", "mcp__srv__x"):
        result.register(Tool(name), read_only=name in {"read_file", "find_files", "search_code"}, do_visible=True)
    return result


def test_child_tool_policy_is_intersection_deny_mode_and_background_readonly():
    tools = registry()
    scope = SessionScope("intersection", 1)
    launch = ParentPolicySnapshot(scope, frozenset(tools.names()), PermissionMode.TRUSTED)
    latest = ParentPolicySnapshot(scope, frozenset({"read_file", "write_file", "mcp__srv__x"}), PermissionMode.DEFAULT)

    actual = child_tool_names(
        tools,
        launch=launch,
        latest=latest,
        allow={"read_file", "write_file", "find_files", "agent", "load_skill", "mcp__srv__x"},
        deny={"find_files"},
        mode=AgentMode.DO,
    )
    assert actual == {"read_file", "write_file", "mcp__srv__x"}
    assert child_tool_names(
        tools, launch=launch, latest=latest, allow=tools.names(), mode=AgentMode.PLAN,
    ) == PLAN_TOOL_NAMES & set(tools.names()) & set(latest.visible_tools)
    assert child_tool_names(
        tools, launch=launch, latest=latest, allow=tools.names(), execution=TaskExecution.BACKGROUND,
    ) == {"read_file"}


def test_policy_snapshot_is_immutable_and_updates_can_only_narrow():
    manager = SubAgentManager(lambda _: WorkerResult())
    scope = manager.open_session("policy-monotonic")
    try:
        original_names = {"read_file", "write_file", "search_code", "find_files"}
        manager.publish_policy_snapshot(scope, ParentPolicySnapshot(scope, original_names, PermissionMode.TRUSTED))
        original_names.clear()
        launch = manager.start(scope, "task")
        launch_snapshot = manager._tasks[launch.task_id].launch_policy
        assert launch_snapshot.visible_tools == {"read_file", "write_file", "search_code", "find_files"}

        manager.publish_policy_snapshot(
            scope,
            ParentPolicySnapshot(scope, frozenset(PLAN_TOOL_NAMES), PermissionMode.STRICT),
        )
        latest = manager.policy_snapshot(scope)
        assert latest.revision == 1
        assert latest.visible_tools == PLAN_TOOL_NAMES
        assert launch_snapshot.visible_tools != latest.visible_tools
        with pytest.raises(SubAgentManagerError, match="subagent_policy_expansion_rejected"):
            manager.publish_policy_snapshot(
                scope,
                ParentPolicySnapshot(scope, frozenset({"read_file", "write_file"}), PermissionMode.STRICT),
            )
    finally:
        manager.shutdown()


def test_only_manager_creator_thread_can_publish_parent_policy():
    manager = SubAgentManager(lambda _: WorkerResult())
    scope = manager.open_session("policy-owner")
    manager.publish_policy_snapshot(scope, ParentPolicySnapshot(scope, frozenset({"read_file"}), PermissionMode.DEFAULT))
    errors = []

    def publish_from_worker():
        try:
            manager.publish_policy_snapshot(scope, ParentPolicySnapshot(scope, frozenset(), PermissionMode.STRICT))
        except SubAgentManagerError as exc:
            errors.append(exc.code)

    thread = threading.Thread(target=publish_from_worker)
    thread.start()
    thread.join(1)
    try:
        assert errors == ["subagent_policy_publish_thread_invalid"]
        assert manager.policy_snapshot(scope).visible_tools == {"read_file"}
    finally:
        manager.shutdown()


def test_policy_is_revoked_when_session_scope_closes():
    manager = SubAgentManager(lambda _: WorkerResult())
    scope = manager.open_session("policy-revoke")
    manager.publish_policy_snapshot(scope, ParentPolicySnapshot(scope, frozenset({"read_file"}), PermissionMode.DEFAULT))
    manager.close_session(scope)
    try:
        with pytest.raises(SubAgentManagerError, match="subagent_parent_session_closed"):
            manager.policy_snapshot(scope)
    finally:
        manager.shutdown()
