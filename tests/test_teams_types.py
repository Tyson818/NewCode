from dataclasses import FrozenInstanceError, replace
import json

import pytest

from newcode.teams import types as t


def manifest():
    lead = t.TeamMemberRecord("lead", "Lead", role=t.TeamRole.LEAD)
    now = t.utc_now()
    return t.TeamManifest(t.new_id("team"), "设计团队", "a" * 64, "lead", (lead,), now, now)


def test_manifest_roundtrip_strict_fields_and_runtime_objects_rejected():
    value = manifest()
    assert t.TeamManifest.from_dict(json.loads(json.dumps(value.to_dict()))) == value
    with pytest.raises(t.TeamError):
        t.TeamManifest.from_dict({**value.to_dict(), "provider": object()})
    with pytest.raises(t.TeamError):
        t.freeze({"client": object()})
    with pytest.raises(t.TeamError):
        t.freeze(float("nan"))
    with pytest.raises(FrozenInstanceError):
        value.revision = 2


@pytest.mark.parametrize("enum", [t.TeamState, t.MemberState, t.TaskState, t.PlanState])
def test_every_state_pair_has_fixed_transition_result(enum):
    mapping = {t.TeamState: t._TEAM_TRANSITIONS, t.MemberState: t._MEMBER_TRANSITIONS,
               t.TaskState: t._TASK_TRANSITIONS, t.PlanState: t._PLAN_TRANSITIONS}[enum]
    for current in enum:
        for target in enum:
            if target.value in mapping[current.value]:
                t.validate_transition(current, target)
            else:
                with pytest.raises(t.TeamError, match="team_state_invalid"):
                    t.validate_transition(current, target)
    with pytest.raises(t.TeamError):
        t.validate_transition(enum(next(iter(enum))), t.Backend.LIGHTWEIGHT)


def test_recoverable_and_terminal_states_are_distinct_from_agent_mode():
    t.validate_transition(t.MemberState.PAUSED, t.MemberState.RESUMING)
    t.validate_transition(t.MemberState.INTERRUPTED, t.MemberState.STARTING)
    t.validate_transition(t.MemberState.RUNNING, t.MemberState.AWAITING_APPROVAL)
    with pytest.raises(t.TeamError):
        t.validate_transition(t.MemberState.INTEGRATED, t.MemberState.RESUMING)
    assert t.MemberState.AWAITING_APPROVAL.value != "plan"


def test_roster_casefold_collision_and_limits():
    value = manifest()
    with pytest.raises(t.TeamError, match="team_name_conflict"):
        replace(value, roster=(value.roster[0], t.TeamMemberRecord("worker", "LEAD", agent_name="reader")))
    members = tuple(t.TeamMemberRecord(f"m{i}", f"m{i}", agent_name="reader") for i in range(9))
    with pytest.raises(t.TeamError, match="team_limit_exceeded"):
        replace(value, roster=(value.roster[0], *members))
    with pytest.raises(t.TeamError):
        replace(value, revision=True)


def test_task_message_plan_roundtrip_and_immutable_payload():
    now = t.utc_now()
    task = t.TeamTask(t.new_id("task"), "任务", "", "lead", now, now)
    message = t.TeamMessage(t.new_id("message"), manifest().team_id, "lead", "lead", "内容", now, 1, "test", payload={"list": [1]})
    plan = t.TeamPlan(t.new_id("plan"), "lead", task.task_id, "计划", now)
    for value in (task, message, plan):
        assert type(value).from_dict(value.to_dict()) == value
    with pytest.raises(TypeError):
        message.payload["x"] = 1
    with pytest.raises(t.TeamError):
        replace(task, dependencies=(task.task_id,))
    with pytest.raises(t.TeamError):
        replace(message, body="中" * (t.MAX_MESSAGE_BYTES // 3 + 1))
    with pytest.raises(t.TeamError):
        replace(plan, state=t.PlanState.APPROVED)


def test_policy_startup_ceiling_is_not_changed_by_approval():
    scope = t.TeamScope(manifest().team_id, "lead", "20261016-083000-ab12", 1)
    policy = t.TeamPolicySnapshot(scope, 1, frozenset({"read_file", "write_file"}), frozenset({"read_file"}))
    approved = replace(policy, plan_state=t.PlanState.APPROVED, plan_revision=1)
    assert approved.current_tools == {"read_file"}
    assert approved.startup_tools == policy.startup_tools
    assert t.TeamPolicySnapshot.from_dict(approved.to_dict()) == approved
    with pytest.raises(t.TeamError, match="team_policy_changed"):
        replace(policy, current_tools=frozenset({"run_command"}))
    with pytest.raises(t.TeamError, match="team_policy_changed"):
        t.validate_policy_update(policy, replace(approved, revision=2, current_tools=policy.startup_tools))
    t.validate_policy_update(policy, replace(approved, revision=2))


def test_resource_caps_errors_and_id_generation():
    assert (t.MAX_MEMBERS, t.MAX_RUNNING_MEMBERS, t.MAX_QUEUED_MEMBERS) == (8, 4, 16)
    assert (t.MAX_TASKS, t.MAX_MESSAGES, t.MAX_PENDING_MESSAGES) == (256, 4096, 1024)
    assert (t.MAX_MEMBER_ROUNDS, t.MAX_MEMBER_SECONDS, t.MAX_MEMBER_TOKENS) == (8, 300, 16000)
    assert (t.MAX_TRANSACTIONS, t.MAX_TRANSACTION_BYTES, t.MAX_JOURNAL_BYTES) == (8192, 512 * 1024, 64 * 1024 * 1024)
    assert t.MAX_CHECKPOINT_BYTES == 8 * 1024 * 1024 and t.SHUTDOWN_SECONDS == 1
    assert len({t.new_id("team") for _ in range(32)}) == 32
    assert str(t.TeamError("unknown secret")) == "team_arguments_invalid"
    assert "team_ack_checkpoint_required" in t.ERROR_CODES


@pytest.mark.parametrize("name", ["..", "../a", "C:evil", "a/b", "a\\b", "con", "NUL", "", "x" * 33])
def test_names_reject_unsafe_forms(name):
    with pytest.raises(t.TeamError):
        t.validate_name(name)
