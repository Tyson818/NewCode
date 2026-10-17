from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from newcode.context.estimator import estimate_messages
from newcode.session import ChatMessage, ChatSession
from newcode.teams.member import MemberCheckpoints, delivery_message
from newcode.teams.store import TeamStore
from newcode.teams.types import RecordChange, TeamError, TeamMemberRecord, TeamMessage, new_id, utc_now
from newcode.tools.types import ToolCall, ToolResult
from test_teams_paths import make_paths
from test_teams_store import child_process

NOW = datetime(2026, 10, 16, 8, 30, tzinfo=timezone.utc)


def setup_checkpoint(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths, sensitive_values=("fixture-secret",))
    team = store.create_team("checkpoint team")
    member = TeamMemberRecord("alice", "alice", agent_name="reader")
    store.commit_transaction(team.team_id, expected_revision=1, mutation_id=new_id("mutation"),
                             changes=(RecordChange("members", member.member_id, member.to_dict()),))
    return store, team.team_id, MemberCheckpoints(store, team.team_id, "alice", generation=1)


def add_mail(store, team_id, recipient="alice", sequence=1):
    team = store.load_team(team_id)
    message = TeamMessage(new_id("message"), team_id, "lead", recipient, "safe body", utc_now(), sequence, f"key{sequence}")
    store.commit_transaction(team_id, expected_revision=team.revision, mutation_id=new_id("mutation"),
                             changes=(RecordChange("messages", message.message_id, message.to_dict()),))
    return message


def checkpoint_path(checkpoints, session_id):
    return checkpoints.store.paths.root.joinpath(*checkpoints._parts(session_id))


def test_checkpoint_roundtrip_redacts_and_does_not_mutate_live_history(tmp_path):
    store, team_id, cp = setup_checkpoint(tmp_path)
    session = ChatSession()
    session.add_user_message("keep fixture-secret https://alice:pass@example.test/path")
    call = ToolCall("call-1", "read_file", {"path": "safe.txt", "headers": {"Authorization": "unknown-credential"}}, "raw fixture-secret")
    session.add_assistant_tool_call(call)
    session.add_tool_result("call-1", ToolResult.success("read_file", {"env": {"API_KEY": "unknown-secret"}, "content": "ok"}))
    session.add_assistant_message("done")
    original = list(session.messages)
    session.permission_session_allow = {"read_file"}
    proof = cp.checkpoint(session, now=NOW)
    raw = checkpoint_path(cp, proof.session_id).read_text(encoding="utf-8")
    assert "fixture-secret" not in raw and "unknown-secret" not in raw and "unknown-credential" not in raw
    assert "alice:pass" not in raw and "permission_session_allow" not in raw
    assert session.messages == original
    restored = cp.restore(proof.session_id, now=NOW)
    assert restored.session is not session and restored.errors == ()
    assert [m.role for m in restored.session.messages] == ["user", "assistant", "tool", "assistant"]
    assert restored.session.messages[-1].content == "done"
    assert not hasattr(restored.session, "permission_session_allow")
    assert restored.estimated_tokens == estimate_messages(restored.session.messages)
    assert not restored.needs_compaction
    # 同一 JSONL 仍能被 Chapter9 归档器恢复；扩展元数据不会进入消息。
    from newcode.persistence import SessionArchive
    archive = SessionArchive(store.paths.workspace_root, sessions_root=checkpoint_path(cp, proof.session_id).parent)
    assert archive.restore(proof.session_id, now=NOW).session.messages == restored.session.messages


def test_valid_checkpoint_and_processed_ids_are_one_publish_unit(tmp_path):
    store, team_id, cp = setup_checkpoint(tmp_path)
    mail = add_mail(store, team_id)
    session = ChatSession([delivery_message(mail.to_dict())])
    proof = cp.checkpoint(session, processed_message_ids=(mail.message_id,), now=NOW)
    assert cp.proof_for_ack(proof.session_id, mail.message_id, now=NOW) == proof
    raw = checkpoint_path(cp, proof.session_id).read_text(encoding="utf-8")
    metadata = json.loads(raw.splitlines()[0])
    assert metadata["team_checkpoint"]["proof"]["processed_message_ids"] == [mail.message_id]
    assert raw.count(mail.message_id) == 2
    # Context 压缩后正文可消失，但已持久交付的证明仍保留。
    session.replace_messages([ChatMessage("system", "structured summary"), ChatMessage("user", "continue")])
    cp.checkpoint(session, now=NOW)
    assert cp.proof_for_ack(proof.session_id, mail.message_id, now=NOW).processed_message_ids == (mail.message_id,)


def test_unobserved_other_member_and_wrong_generation_proofs_refused(tmp_path):
    store, team_id, cp = setup_checkpoint(tmp_path)
    own = add_mail(store, team_id)
    others = add_mail(store, team_id, "lead", 2)
    session = ChatSession([ChatMessage("user", "not delivered")])
    with pytest.raises(TeamError, match="team_ack_checkpoint_required"):
        cp.checkpoint(session, processed_message_ids=(own.message_id,), now=NOW)
    assert session.session_id is None
    with pytest.raises(TeamError, match="team_message_not_owned"):
        cp.checkpoint(ChatSession([delivery_message(others.to_dict())]), processed_message_ids=(others.message_id,), now=NOW)
    session = ChatSession([delivery_message(own.to_dict())])
    proof = cp.checkpoint(session, processed_message_ids=(own.message_id,), now=NOW)
    next_cp = MemberCheckpoints(store, team_id, "alice", generation=2)
    with pytest.raises(TeamError, match="team_ack_checkpoint_required"):
        next_cp.proof_for_ack(proof.session_id, own.message_id, now=NOW)
    restored = next_cp.restore(proof.session_id, now=NOW)
    next_cp.checkpoint(restored.session, now=NOW)
    assert next_cp.proof_for_ack(proof.session_id, own.message_id, now=NOW).generation == 2
    assert cp.restore(proof.session_id, now=NOW).session is None


def test_team_message_read_observation_is_persisted_but_not_acked(tmp_path):
    store, team_id, cp = setup_checkpoint(tmp_path)
    mail = add_mail(store, team_id)
    session = ChatSession()
    session.add_assistant_tool_call(ToolCall("read-mail", "team_message", {"operation": "read"}))
    session.add_tool_result("read-mail", ToolResult.success("team_message", {"messages": [mail.to_dict()]}))
    cp.checkpoint(session, processed_message_ids=(mail.message_id,), now=NOW)
    assert cp.proof_for_ack(session.session_id, mail.message_id, now=NOW)
    assert not (store.paths.team(team_id) / "acks").exists()
    assert store.load_records(team_id, "messages")[0]["message_id"] == mail.message_id


@pytest.mark.parametrize("extra, expected", [
    ([{"record_type": "message", "message": {"role": "tool", "content": "{}", "tool_call_id": "orphan"}}], ["before"]),
    ([{"record_type": "message", "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "missing", "name": "read_file", "arguments": {}, "raw_arguments": "{}"}]}}], ["before"]),
])
def test_recovery_truncates_first_invalid_exchange_and_invalidates_delivery(tmp_path, extra, expected):
    _, _, cp = setup_checkpoint(tmp_path)
    session = ChatSession([ChatMessage("user", "before")])
    cp.checkpoint(session, now=NOW)
    path = checkpoint_path(cp, session.session_id)
    with path.open("a", encoding="utf-8") as stream:
        for record in extra:
            stream.write(json.dumps(record) + "\n")
    before = path.read_bytes()
    result = cp.restore(session.session_id, now=NOW)
    assert [m.content for m in result.session.messages] == expected
    assert result.truncated and result.proof is None
    assert "session_tool_pair_truncated" in result.errors
    assert path.read_bytes() == before


def test_bad_lines_unknown_records_recover_content_but_cannot_ack(tmp_path):
    store, team_id, cp = setup_checkpoint(tmp_path)
    mail = add_mail(store, team_id)
    session = ChatSession([delivery_message(mail.to_dict())])
    cp.checkpoint(session, processed_message_ids=(mail.message_id,), now=NOW)
    path = checkpoint_path(cp, session.session_id)
    with path.open("a", encoding="utf-8") as stream:
        stream.write('bad-json\n{"record_type":"unknown"}\n')
    result = cp.restore(session.session_id, now=NOW)
    assert result.session.messages == session.messages
    assert "session_record_invalid" in result.errors and result.proof is None
    with pytest.raises(TeamError, match="team_ack_checkpoint_required"):
        cp.proof_for_ack(session.session_id, mail.message_id, now=NOW)


def test_failed_checkpoint_keeps_previous_file_and_leaves_no_temp(tmp_path):
    store, _, cp = setup_checkpoint(tmp_path)
    session = ChatSession([ChatMessage("user", "original")])
    cp.checkpoint(session, now=NOW)
    path = checkpoint_path(cp, session.session_id)
    before = path.read_bytes()
    session.add_user_message("new data")
    def fail(stage):
        if stage == "before_publish":
            raise TeamError("team_store_unavailable")
    store._fault = fail
    with pytest.raises(TeamError, match="team_store_unavailable"):
        cp.checkpoint(session, now=NOW)
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.tmp")) == []
    assert list(path.parent.glob(".*.tmp")) == []


def test_restore_time_reminder_expiry_scope_and_budget(tmp_path):
    store, team_id, cp = setup_checkpoint(tmp_path)
    session = ChatSession([ChatMessage("user", "a" * 100000)])
    cp.checkpoint(session, now=NOW)
    restored = cp.restore(session.session_id, now=NOW + timedelta(hours=24))
    assert restored.needs_time_span_reminder and restored.needs_compaction and restored.estimated_tokens >= 51000
    assert cp.restore(session.session_id, now=NOW + timedelta(days=31)).session is None
    assert cp.restore("../escape", now=NOW).session is None
    lead = MemberCheckpoints(store, team_id, "lead", generation=1)
    assert lead.restore(session.session_id, now=NOW).session is None
    other = store.create_team("other")
    assert MemberCheckpoints(store, other.team_id, "lead", generation=1).restore(session.session_id, now=NOW).session is None


def test_checkpoint_rejects_runtime_arguments_and_size_without_overwrite(tmp_path, monkeypatch):
    import newcode.teams.member as module
    _, _, cp = setup_checkpoint(tmp_path)
    session = ChatSession([ChatMessage("user", "safe")])
    cp.checkpoint(session, now=NOW)
    path = checkpoint_path(cp, session.session_id)
    before = path.read_bytes()
    monkeypatch.setattr(module, "MAX_CHECKPOINT_BYTES", len(before))
    session.add_user_message("larger new message")
    with pytest.raises(TeamError, match="team_limit_exceeded"):
        cp.checkpoint(session, now=NOW)
    assert path.read_bytes() == before
    unsafe = ChatSession()
    unsafe.add_assistant_tool_call(ToolCall("call", "read_file", {"runtime": object()}))
    with pytest.raises(TeamError):
        cp.checkpoint(unsafe, now=NOW)


@pytest.mark.parametrize("stage", ["before_checkpoint", "after_checkpoint"])
def test_actual_checkpoint_process_crash_preserves_complete_snapshot(tmp_path, stage):
    store, team_id, cp = setup_checkpoint(tmp_path)
    mail = add_mail(store, team_id)
    session = ChatSession([delivery_message(mail.to_dict())])
    proof = cp.checkpoint(session, processed_message_ids=(mail.message_id,), now=NOW)
    child = child_process(store.paths, team_id, """
from datetime import datetime
from newcode.teams.member import MemberCheckpoints
cp = MemberCheckpoints(store, team_id, 'alice', generation=1)
now = datetime.fromisoformat(sys.argv[7])
session = cp.restore(sys.argv[5], now=now).session
session.add_user_message('after restart')
store._fault = lambda stage: os._exit(74) if stage == sys.argv[6] else None
cp.checkpoint(session, now=now)
""", proof.session_id, stage, NOW.isoformat())
    _, stderr = child.communicate(timeout=10)
    assert child.returncode == 74 and not stderr
    restored = cp.restore(proof.session_id, now=NOW)
    assert restored.errors == () and restored.proof.processed_message_ids == (mail.message_id,)
    assert len(restored.session.messages) == (1 if stage == "before_checkpoint" else 2)
    if stage == "after_checkpoint":
        assert restored.session.messages[-1].content == "after restart"


def test_tampered_delivery_metadata_is_not_valid_ack_proof(tmp_path):
    store, team_id, cp = setup_checkpoint(tmp_path)
    mail = add_mail(store, team_id)
    session = ChatSession([ChatMessage("user", "did not see this message")])
    cp.checkpoint(session, now=NOW)
    path = checkpoint_path(cp, session.session_id)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    metadata = json.loads(lines[0])
    metadata["team_checkpoint"]["proof"]["processed_message_ids"] = [mail.message_id]
    lines[0] = json.dumps(metadata) + "\n"
    path.write_text("".join(lines), encoding="utf-8", newline="\n")
    assert cp.restore(session.session_id, now=NOW).session.messages[0].content == "did not see this message"
    with pytest.raises(TeamError, match="team_ack_checkpoint_required"):
        cp.proof_for_ack(session.session_id, mail.message_id, now=NOW)
