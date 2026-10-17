"""固定契约的逐 operation 校验与角色矩阵。"""
import copy
from types import SimpleNamespace

import pytest

from newcode.agent.mode import AgentMode, allowed_tool_names
from newcode.agent.scheduler import ToolScheduler
from newcode.teams.tools import (OPERATIONS, TEAM_TOOLS, MEMBER_TOOLS, TeamTool,
                                register_team_tools, tool_schema, validate_arguments)
from newcode.teams.types import TeamError
from newcode.tools.registry import create_default_registry
from newcode.tools.types import ToolCall, ToolContext


def arguments(name, operation):
    values = {
        "team_name": "团队", "backend": "lightweight", "member_name": "alice", "agent_name": "reader",
        "task_id": "task-" + "a" * 32, "plan_id": "plan-" + "b" * 32,
        "message_id": "message-" + "c" * 32, "target_member_id": "alice", "recipient": "alice",
        "title": "任务", "description": "", "dependencies": [], "changes": {"status": "in_progress"},
        "content": "计划", "reason": "理由", "summary": "完成", "body": "消息", "idempotency_key": "key",
        "expected_target_head": "a" * 40, "snapshot_digest": "b" * 64, "confirmation": True,
    }
    contract = OPERATIONS[name][operation]
    return {"operation": operation, **{k: (1 if k.endswith("revision") else values[k]) for k in contract.required}}


CASES = [(name, operation) for name, operations in OPERATIONS.items() for operation in operations]


@pytest.mark.parametrize("name,operation", CASES)
def test_every_operation_fixed_schema_and_backend_required_fields(name, operation):
    contract = OPERATIONS[name][operation]
    args = arguments(name, operation)
    role = contract.roles[0]
    assert validate_arguments(name, args, role) == operation
    branch = next(x for x in tool_schema(name)["oneOf"] if x["properties"]["operation"]["const"] == operation)
    assert branch["additionalProperties"] is False
    assert set(branch["required"]) == {"operation", *contract.required}
    assert set(branch["properties"]) == {"operation", *contract.required, *contract.optional}
    for field in ["operation", *contract.required]:
        bad = dict(args)
        bad.pop(field)
        with pytest.raises(TeamError):
            validate_arguments(name, bad, role)
    for identity in ("sender", "role", "member_id", "team_id", "decision_maker", "generation", "kind"):
        with pytest.raises(TeamError, match="team_arguments_invalid"):
            validate_arguments(name, dict(args, **{identity: "lead"}), role)
    for other_role in {"ordinary", "subagent", "member", "lead"} - set(contract.roles):
        with pytest.raises(TeamError, match="team_role_denied"):
            validate_arguments(name, args, other_role)


@pytest.mark.parametrize("role,expected", [("ordinary", set()), ("subagent", set()),
                                           ("member", MEMBER_TOOLS), ("lead", TEAM_TOOLS)])
def test_role_registration_and_scheduler(role, expected):
    registry = create_default_registry()
    register_team_tools(registry, SimpleNamespace(role=role))
    assert TEAM_TOOLS & set(registry.names()) == set(expected)
    assert TEAM_TOOLS & allowed_tool_names(AgentMode.PLAN, registry) == ({"team_status"} if expected else set())
    for name in expected:
        assert registry.is_read_only(name) == (name == "team_status")
        if name != "team_status":
            assert not ToolScheduler(registry).make_batches([ToolCall("x", name)])[0].parallel


@pytest.mark.parametrize("changes", [{"status": "cancelled"}, {"title": "冒充"}, {"status": "in_progress", "owner": "lead"}])
def test_member_update_cannot_escalate(changes):
    args = arguments("team_tasks", "update")
    args["changes"] = changes
    with pytest.raises(TeamError):
        validate_arguments("team_tasks", args, "member")


@pytest.mark.parametrize("name,operation", CASES)
def test_tool_does_not_delegate_invalid_arguments(name, operation, tmp_path):
    calls = []
    runtime = SimpleNamespace(role=OPERATIONS[name][operation].roles[0],
                              invoke=lambda *args: calls.append(args))
    tool = TeamTool(name, runtime)
    bad = dict(arguments(name, operation), team_id="forged")
    result = tool.run(bad, ToolContext(tmp_path))
    assert not result.ok and result.error.code == "team_arguments_invalid" and calls == []


@pytest.mark.parametrize("key,value", [("expected_task_revision", True), ("expected_task_revision", 0),
                                      ("dependencies", ["bad"]), ("description", "密" * 3000)])
def test_field_validation_is_not_only_schema(key, value):
    name, op = ("team_tasks", "claim") if key == "expected_task_revision" else ("team_tasks", "create")
    args = arguments(name, op)
    args[key] = value
    with pytest.raises(TeamError):
        validate_arguments(name, args, OPERATIONS[name][op].roles[0])


def test_plan_optional_pair_and_cross_operation_fields():
    args = arguments("team_tasks", "submit_plan")
    args["plan_id"] = "plan-" + "a" * 32
    with pytest.raises(TeamError):
        validate_arguments("team_tasks", args, "member")
    with pytest.raises(TeamError):
        validate_arguments("team_status", {"operation": "tasks", "task_id": "task-" + "a" * 32, "limit": 1}, "lead")


def test_ordinary_subagent_registry_view_does_not_inherit_lead_role():
    from newcode.subagents.policy import isolated_registry_view
    source = create_default_registry()
    register_team_tools(source, SimpleNamespace(role="lead"))
    child = isolated_registry_view(source, set(source.names()), {})
    assert not TEAM_TOOLS & set(child.names())
    assert child.execution_role == "ordinary"
    wrappers = {name: SimpleNamespace(spec=source.get(name).spec) for name in TEAM_TOOLS}
    wrapped_child = isolated_registry_view(source, set(source.names()), wrappers)
    assert not TEAM_TOOLS & set(wrapped_child.names())
