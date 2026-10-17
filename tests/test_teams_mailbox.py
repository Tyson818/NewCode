import json
import subprocess
import sys

import pytest

from newcode.session import ChatSession
from newcode.teams.mailbox import MailboxService
from newcode.teams.member import MemberCheckpoints, delivery_message
from newcode.teams.paths import TeamPaths
from newcode.teams.store import TeamStore
from newcode.teams.tasks import TaskService
from newcode.teams.types import MessageKind, RecordChange, TeamError, TeamMemberRecord, new_id
from test_teams_paths import make_paths


def setup_mailbox(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths, sensitive_values=("mail-secret-value",))
    team = store.create_team("mailbox tests")
    workers = tuple(TeamMemberRecord(name, name, agent_name="reader") for name in ("alice", "bob"))
    store.commit_transaction(team.team_id, expected_revision=1, mutation_id=new_id("mutation"),
                             changes=tuple(RecordChange("members", item.member_id, item.to_dict()) for item in workers))
    return paths, store, team


def test_send_uses_runtime_sender_redacts_and_read_does_not_ack(tmp_path):
    _, store, team = setup_mailbox(tmp_path)
    wake_calls = []
    lead = MailboxService(store, team.team_id, "lead", wake=wake_calls.append)
    result = lead.send("alice", "mail-secret-value", "send-1")
    message = result.messages[0]
    assert (message.sender_member_id, message.recipient, message.kind) == ("lead", "alice", MessageKind.MESSAGE)
    assert message.body == "[REDACTED]"
    assert wake_calls == ["alice"] and not result.wake_failed
    alice = MailboxService(store, team.team_id, "alice")
    assert alice.read().messages == (message,)
    assert alice.read().messages == (message,)


def test_send_idempotency_and_wake_failure_do_not_lose_committed_mail(tmp_path):
    _, store, team = setup_mailbox(tmp_path)

    def fail_wake(_recipient):
        raise RuntimeError("sensitive wake failure")

    lead = MailboxService(store, team.team_id, "lead", wake=fail_wake)
    first = lead.send("alice", "hello", "stable-key")
    retry = lead.send("alice", "hello", "stable-key")
    assert first.messages == retry.messages
    assert first.wake_failed and retry.wake_failed
    assert len(store.load_records(team.team_id, "messages")) == 1
    with pytest.raises(TeamError, match="team_idempotency_conflict"):
        lead.send("alice", "different", "stable-key")
    assert len(store.load_records(team.team_id, "messages")) == 1


def test_broadcast_has_stable_recipients_and_replay_does_not_duplicate(tmp_path):
    _, store, team = setup_mailbox(tmp_path)
    lead = MailboxService(store, team.team_id, "lead")
    first = lead.broadcast("status update", "broadcast-1")
    replay = lead.broadcast("status update", "broadcast-1")
    assert first.messages == replay.messages
    assert [item.recipient for item in first.messages] == ["alice", "bob"]
    assert all(item.broadcast_id == first.messages[0].broadcast_id for item in first.messages)
    assert len(store.load_records(team.team_id, "messages")) == 2
    assert len(MailboxService(store, team.team_id, "alice").read().messages) == 1
    assert len(MailboxService(store, team.team_id, "bob").read().messages) == 1


def test_cursor_is_bound_to_member_and_read_limit(tmp_path):
    _, store, team = setup_mailbox(tmp_path)
    lead = MailboxService(store, team.team_id, "lead")
    for index in range(3):
        lead.send("alice", f"message {index}", f"k-{index}")
    alice = MailboxService(store, team.team_id, "alice", cursor_key=b"k" * 32)
    page = alice.read(limit=2)
    assert len(page.messages) == 2 and page.next_cursor
    assert [message.body for message in alice.read(limit=2, cursor=page.next_cursor).messages] == ["message 2"]
    bob = MailboxService(store, team.team_id, "bob", cursor_key=b"k" * 32)
    with pytest.raises(TeamError, match="team_cursor_invalid"):
        bob.read(cursor=page.next_cursor)


def test_ack_requires_checkpoint_containing_owned_message_and_is_idempotent(tmp_path):
    paths, store, team = setup_mailbox(tmp_path)
    message = MailboxService(store, team.team_id, "lead").send("alice", "deliver me", "ack-1").messages[0]
    checkpoints = MemberCheckpoints(store, team.team_id, "alice", generation=3)
    alice = MailboxService(store, team.team_id, "alice")
    with pytest.raises(TeamError, match="team_ack_checkpoint_required"):
        alice.ack(message.message_id, session_id="20260101-010101-ab12", checkpoints=checkpoints)
    assert len(alice.read().messages) == 1
    session = ChatSession([delivery_message(message.to_dict())], "20260101-010101-ab12")
    proof = checkpoints.checkpoint(session, processed_message_ids=(message.message_id,))
    assert proof.generation == 3
    assert alice.ack(message.message_id, session_id=session.session_id, checkpoints=checkpoints) is True
    assert alice.ack(message.message_id, session_id=session.session_id, checkpoints=checkpoints) is True
    assert alice.read().messages == ()
    ack_projection = paths.team(team.team_id) / "acks" / "alice.json"
    assert message.message_id in json.loads(ack_projection.read_text(encoding="utf-8"))["acknowledged_message_ids"]


@pytest.mark.parametrize("fault_stage", ["before_checkpoint", "after_checkpoint"])
def test_delivery_checkpoint_failure_or_crash_retry_never_duplicates_session_message(tmp_path, fault_stage):
    _, store, team = setup_mailbox(tmp_path)
    checkpoints = MemberCheckpoints(store, team.team_id, "alice", generation=4)
    session = ChatSession([], "20260103-030303-dd44")
    checkpoints.checkpoint(session)
    message = MailboxService(store, team.team_id, "lead").send("alice", "once", "deliver-once").messages[0]
    alice = MailboxService(store, team.team_id, "alice")

    def crash_at(stage):
        if stage == fault_stage:
            raise RuntimeError("injected checkpoint interruption")

    store._fault = crash_at
    with pytest.raises(RuntimeError):
        alice.deliver_pending(session, checkpoints)
    assert sum(item == delivery_message(message.to_dict()) for item in session.messages) == 1
    assert len(alice.read().messages) == 1  # 发布失败或ack未执行，邮箱仍可重试

    store._fault = lambda _stage: None
    batch = alice.deliver_pending(session, checkpoints)
    assert message.message_id in batch.processed_message_ids
    assert sum(item == delivery_message(message.to_dict()) for item in session.messages) == 1
    assert alice.read().messages == ()


def test_checkpoint_must_belong_to_current_recipient_and_generation(tmp_path):
    _, store, team = setup_mailbox(tmp_path)
    message = MailboxService(store, team.team_id, "lead").send("alice", "owned by alice", "owner-1").messages[0]
    bob_points = MemberCheckpoints(store, team.team_id, "bob", generation=1)
    with pytest.raises(TeamError, match="team_message_not_owned"):
        MailboxService(store, team.team_id, "bob").ack(
            message.message_id, session_id="20260101-010101-bb22", checkpoints=bob_points)
    old_generation = MemberCheckpoints(store, team.team_id, "alice", generation=1)
    session = ChatSession([delivery_message(message.to_dict())], "20260101-010102-aa11")
    old_generation.checkpoint(session, processed_message_ids=(message.message_id,))
    current_generation = MemberCheckpoints(store, team.team_id, "alice", generation=2)
    with pytest.raises(TeamError, match="team_ack_checkpoint_required"):
        MailboxService(store, team.team_id, "alice").ack(
            message.message_id, session_id=session.session_id, checkpoints=current_generation)


def test_model_send_cannot_select_system_kind_and_task_service_emits_trusted_event(tmp_path):
    _, store, team = setup_mailbox(tmp_path)
    lead_mail = MailboxService(store, team.team_id, "lead")
    with pytest.raises(TypeError):
        lead_mail.send("alice", "forged", "forged-1", kind=MessageKind.TASK_COMPLETED)
    task_service = TaskService(store, team.team_id)
    task = task_service.create("lead", title="work")
    task_service.assign("lead", task.task_id, "alice", expected_revision=1)
    messages = store.load_records(team.team_id, "messages")
    assert len(messages) == 1
    event = messages[0]
    assert event["kind"] == MessageKind.TASK_ASSIGNED.value
    assert event["sender_member_id"] == "lead" and event["recipient"] == "alice"


def _crash_worker(paths, team_id, code, *args):
    script = """
import sys
from pathlib import Path
from newcode.teams.paths import TeamPaths
from newcode.teams.store import TeamStore
workspace, common, home, team_id = sys.argv[1:5]
store = TeamStore(TeamPaths(Path(workspace), common_git_dir=Path(common), user_home=Path(home)))
exec(sys.argv[5])
"""
    return subprocess.run([sys.executable, "-c", script, str(paths.workspace_root), str(paths.common_git_dir),
                           str(paths.home), team_id, code, *args], capture_output=True, text=True, timeout=25)


@pytest.mark.parametrize(("stage", "committed"), [
    ("before_commit", False), ("after_commit", True), ("after_projection:messages", True),
])
def test_send_crash_boundary_recovers_committed_message_once(tmp_path, stage, committed):
    paths, store, team = setup_mailbox(tmp_path)
    code = """
import os, sys
from newcode.teams.mailbox import MailboxService
stage = sys.argv[6]
store._fault = lambda current: os._exit(71) if current == stage else None
MailboxService(store, team_id, "lead").send("alice", "durable", "crash-send")
"""
    result = _crash_worker(paths, team.team_id, code, stage)
    assert result.returncode == 71 and not result.stderr
    inbox = MailboxService(store, team.team_id, "alice").read().messages
    assert len(inbox) == (1 if committed else 0)
    if committed:
        replay = MailboxService(store, team.team_id, "lead").send("alice", "durable", "crash-send")
        assert replay.messages[0].message_id == inbox[0].message_id
        assert len(store.load_records(team.team_id, "messages")) == 1


@pytest.mark.parametrize(("stage", "committed"), [
    ("before_commit", False), ("after_commit", True), ("after_projection:messages", True),
])
def test_broadcast_crash_boundaries_recover_complete_roster_without_duplicates(tmp_path, stage, committed):
    paths, store, team = setup_mailbox(tmp_path)
    code = """
import os
import sys
from newcode.teams.mailbox import MailboxService
fault_stage = sys.argv[6]
store._fault = lambda current: os._exit(72) if current == fault_stage else None
MailboxService(store, team_id, "lead").broadcast("all members", "crash-broadcast")
"""
    result = _crash_worker(paths, team.team_id, code, stage)
    assert result.returncode == 72 and not result.stderr
    records = store.load_records(team.team_id, "messages")
    assert len(records) == (2 if committed else 0)
    if committed:
        assert sorted(item["recipient"] for item in records) == ["alice", "bob"]
        replay = MailboxService(store, team.team_id, "lead").broadcast("all members", "crash-broadcast")
        assert {item.message_id for item in replay.messages} == {item["message_id"] for item in records}
        assert len(store.load_records(team.team_id, "messages")) == 2


@pytest.mark.parametrize(("stage", "acked"), [
    ("before_commit", False), ("after_commit", True), ("after_projection:acks", True),
])
def test_ack_crash_boundary_replays_projection_without_duplicate_delivery(tmp_path, stage, acked):
    paths, store, team = setup_mailbox(tmp_path)
    message = MailboxService(store, team.team_id, "lead").send("alice", "ack after checkpoint", "ack-crash").messages[0]
    checkpoints = MemberCheckpoints(store, team.team_id, "alice", generation=5)
    session = ChatSession([delivery_message(message.to_dict())], "20260202-020202-cc33")
    checkpoints.checkpoint(session, processed_message_ids=(message.message_id,))
    code = """
import os, sys
from newcode.teams.mailbox import MailboxService
from newcode.teams.member import MemberCheckpoints
session_id, stage, message_id = sys.argv[6:9]
store._fault = lambda current: os._exit(73) if current == stage else None
points = MemberCheckpoints(store, team_id, "alice", generation=5)
MailboxService(store, team_id, "alice").ack(message_id, session_id=session_id, checkpoints=points)
"""
    result = _crash_worker(paths, team.team_id, code, session.session_id, stage, message.message_id)
    assert result.returncode == 73 and not result.stderr
    inbox = MailboxService(store, team.team_id, "alice").read().messages
    assert len(inbox) == (0 if acked else 1)
    if acked:
        assert message.message_id in store.load_records(team.team_id, "acks")[0]["acknowledged_message_ids"]
