import subprocess
import sys
from dataclasses import replace

import pytest

from newcode.teams.approvals import ApprovalService
from newcode.teams.mailbox import MailboxService
from newcode.teams.paths import TeamPaths
from newcode.teams.store import TeamStore
from newcode.teams.types import (
    MemberState, MessageKind, PlanState, RecordChange, TeamError,
    TeamMemberRecord, TeamRole, TeamTask, new_id,
)
from test_teams_paths import make_paths


def setup_approval(tmp_path, *, fault=None):
    paths = make_paths(tmp_path)
    store = TeamStore(paths, fault=fault)
    team = store.create_team("approval tests")
    alice = TeamMemberRecord("alice", "alice", agent_name="reader")
    store.commit_transaction(team.team_id, expected_revision=1, mutation_id=new_id("mutation"),
                             changes=(RecordChange("members", alice.member_id, alice.to_dict()),))
    task_service = __import__("newcode.teams.tasks", fromlist=["TaskService"]).TaskService(store, team.team_id)
    task = task_service.create("lead", title="approved work")
    task = task_service.assign("lead", task.task_id, "alice", expected_revision=1)
    member = store.load_records(team.team_id, "members")
    current = TeamMemberRecord.from_dict(next(item for item in member if item["member_id"] == "alice"))
    running = replace(current, state=MemberState.STARTING, revision=current.revision + 1)
    store.commit_transaction(team.team_id, expected_revision=store.load_team(team.team_id).revision,
                             mutation_id=new_id("mutation"),
                             changes=(RecordChange("members", "alice", running.to_dict()),))
    running = replace(running, state=MemberState.RUNNING, revision=running.revision + 1)
    store.commit_transaction(team.team_id, expected_revision=store.load_team(team.team_id).revision,
                             mutation_id=new_id("mutation"),
                             changes=(RecordChange("members", "alice", running.to_dict()),))
    return paths, store, team, task


def plan_of(store, team_id, plan_id):
    return next(item for item in store.load_records(team_id, "plans") if item["plan_id"] == plan_id)


def test_plan_submit_and_structured_approve_are_atomic_and_do_not_grant_permission(tmp_path):
    paths, store, team, task = setup_approval(tmp_path)
    submitted = ApprovalService(store, team.team_id, "alice", mode="do").submit(task.task_id, "step 1")
    assert submitted.state is PlanState.PENDING and submitted.revision == 1
    member = next(item for item in store.load_records(team.team_id, "members") if item["member_id"] == "alice")
    assert member["state"] == MemberState.AWAITING_APPROVAL.value
    tx = sorted((paths.team(team.team_id) / "transactions").glob("*.json"))[-1]
    import json
    committed = json.loads(tx.read_text(encoding="utf-8"))
    collections = {change["collection"] for change in committed["changes"]}
    assert {"plans", "members", "messages"} <= collections
    assert len(store.load_records(team.team_id, "messages")) == 2  # task assignment + submitted plan

    decision = ApprovalService(store, team.team_id, "lead", mode="do").approve(
        submitted.plan_id, 1, reason="reviewed")
    assert decision.state is PlanState.APPROVED
    assert decision.decision_member_id == "lead"
    assert decision.message_id
    member_after = next(item for item in store.load_records(team.team_id, "members") if item["member_id"] == "alice")
    assert member_after["state"] == MemberState.RUNNING.value
    events = store.load_records(team.team_id, "messages")
    assert events[-1]["kind"] == MessageKind.PLAN_DECISION.value
    # 只改 Team 业务状态；不存在 Permission/session-allow 写入路径。
    assert not any(change["collection"] == "permissions" for change in committed["changes"])
    assert plan_of(store, team.team_id, submitted.plan_id)["state"] == PlanState.APPROVED.value


def test_plain_text_agreement_is_only_plan_content_and_plan_mode_writes_nothing(tmp_path):
    _, store, team, task = setup_approval(tmp_path)
    plan = ApprovalService(store, team.team_id, "alice", mode="do").submit(task.task_id, "同意")
    assert plan.state is PlanState.PENDING
    before = sorted(path.name for path in (store.paths.team(team.team_id) / "transactions").glob("*.json"))
    with pytest.raises(TeamError, match="disallowed_tool"):
        ApprovalService(store, team.team_id, "lead", mode="plan").approve(plan.plan_id, 1)
    with pytest.raises(TeamError, match="disallowed_tool"):
        ApprovalService(store, team.team_id, "alice", mode="plan").submit(task.task_id, "another plan")
    assert sorted(path.name for path in (store.paths.team(team.team_id) / "transactions").glob("*.json")) == before
    assert plan_of(store, team.team_id, plan.plan_id)["state"] == PlanState.PENDING.value


def test_revision_reopens_pending_and_old_approval_cannot_be_reused(tmp_path):
    _, store, team, task = setup_approval(tmp_path)
    member = ApprovalService(store, team.team_id, "alice", mode="do")
    lead = ApprovalService(store, team.team_id, "lead", mode="do")
    first = member.submit(task.task_id, "first")
    approved = lead.approve(first.plan_id, first.revision)
    assert approved.state is PlanState.APPROVED
    second = member.submit(task.task_id, "revised", plan_id=first.plan_id,
                           expected_plan_revision=first.revision)
    assert second.plan_id == first.plan_id
    assert second.revision == 2 and second.state is PlanState.PENDING
    with pytest.raises(TeamError, match="team_plan_stale"):
        lead.approve(second.plan_id, expected_plan_revision=1)
    assert plan_of(store, team.team_id, second.plan_id)["state"] == PlanState.PENDING.value
    final = lead.reject(second.plan_id, expected_plan_revision=2, reason="needs changes")
    assert final.state is PlanState.REJECTED and final.revision == 2


def test_member_cannot_decide_plan_and_identity_fields_are_not_parameters(tmp_path):
    _, store, team, task = setup_approval(tmp_path)
    plan = ApprovalService(store, team.team_id, "alice").submit(task.task_id, "proposal")
    with pytest.raises(TeamError, match="team_role_denied"):
        ApprovalService(store, team.team_id, "alice").approve(plan.plan_id, 1)
    with pytest.raises(TypeError):
        ApprovalService(store, team.team_id, "lead").approve(plan.plan_id, 1, decision_member_id="alice")
    with pytest.raises(TypeError):
        ApprovalService(store, team.team_id, "lead").reject(plan.plan_id, 1, reason="no", actor_member_id="alice")
    with pytest.raises(TeamError, match="team_arguments_invalid"):
        ApprovalService(store, team.team_id, "lead").reject(plan.plan_id, 1, reason="")


def test_approval_commit_crash_after_durable_record_replays_once(tmp_path):
    paths, store, team, task = setup_approval(tmp_path)
    plan = ApprovalService(store, team.team_id, "alice").submit(task.task_id, "proposal")
    code = """
import os, sys
from pathlib import Path
from newcode.teams.paths import TeamPaths
from newcode.teams.store import TeamStore
from newcode.teams.approvals import ApprovalService
workspace, common, home, team_id, plan_id = sys.argv[1:6]
store = TeamStore(TeamPaths(Path(workspace), common_git_dir=Path(common), user_home=Path(home)))
store._fault = lambda stage: os._exit(81) if stage == "after_commit" else None
ApprovalService(store, team_id, "lead").approve(plan_id, 1)
"""
    result = subprocess.run([sys.executable, "-c", code, str(paths.workspace_root), str(paths.common_git_dir),
                             str(paths.home), team.team_id, plan.plan_id],
                            capture_output=True, text=True, timeout=25)
    assert result.returncode == 81 and not result.stderr
    recovered = store.load_records(team.team_id, "plans")
    current = next(item for item in recovered if item["plan_id"] == plan.plan_id)
    assert current["state"] == PlanState.APPROVED.value
    decision_events = [item for item in store.load_records(team.team_id, "messages")
                       if item["kind"] == MessageKind.PLAN_DECISION.value]
    assert len(decision_events) == 1
    with pytest.raises(TeamError, match="team_plan_stale"):
        ApprovalService(store, team.team_id, "lead").approve(plan.plan_id, 1)
    assert len([item for item in store.load_records(team.team_id, "messages")
                if item["kind"] == MessageKind.PLAN_DECISION.value]) == 1
