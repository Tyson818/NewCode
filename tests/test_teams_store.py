from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from newcode.teams.store import TeamStore
from newcode.teams.paths import TeamPaths
from newcode.teams.types import RecordChange, TeamError, TeamMemberRecord, TeamMessage, new_id, utc_now
from test_teams_paths import make_paths, make_directory_link


def change_name(team, name="changed"):
    return RecordChange("team", team.team_id, replace(team, name=name, revision=team.revision + 1).to_dict())


def child_process(paths, team_id, body, *arguments):
    code = """
import os, sys
from pathlib import Path
from dataclasses import replace
from newcode.teams.paths import TeamPaths
from newcode.teams.store import TeamStore
from newcode.teams.types import RecordChange, TeamError, new_id
home, workspace, common, team_id = sys.argv[1:5]
paths = TeamPaths(Path(workspace), common_git_dir=Path(common), user_home=Path(home))
store = TeamStore(paths)
""" + body
    return subprocess.Popen([sys.executable, "-c", code, str(paths.home), str(paths.workspace_root), str(paths.common_git_dir), team_id, *arguments],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_real_platform_create_atomic_publish_and_workspace_rejection(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("original")
    assert store.load_team(team.team_id) == team
    assert len(list((paths.team(team.team_id) / "transactions").glob("*.json"))) == 1
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    other = TeamStore(TeamPaths(other_workspace, common_git_dir=paths.common_git_dir, user_home=paths.home))
    with pytest.raises(TeamError, match="team_workspace_mismatch"):
        other.load_team(team.team_id)
    assert json.loads((paths.team(team.team_id) / "team.json").read_bytes())["name"] == "original"


def test_cas_conflict_preserves_old_data_and_idempotent_retry(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("original")
    mutation = new_id("mutation")
    change = change_name(team)
    result = store.commit_transaction(team.team_id, expected_revision=1, mutation_id=mutation, changes=(change,))
    before = (paths.team(team.team_id) / "team.json").read_bytes()
    replay = store.commit_transaction(team.team_id, expected_revision=1, mutation_id=mutation, changes=(change,))
    assert replay.replayed and replay.transaction_id == result.transaction_id
    with pytest.raises(TeamError, match="team_revision_conflict"):
        store.commit_transaction(team.team_id, expected_revision=1, mutation_id=new_id("mutation"), changes=(change_name(team, "wrong"),))
    with pytest.raises(TeamError, match="team_idempotency_conflict"):
        store.commit_transaction(team.team_id, expected_revision=1, mutation_id=mutation, changes=(change_name(team, "different"),))
    assert (paths.team(team.team_id) / "team.json").read_bytes() == before
    assert len(list((paths.team(team.team_id) / "transactions").glob("*.json"))) == 2


def test_real_cross_process_lock_excludes_writer_and_clears_only_released_owner(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("original")
    child = child_process(paths, team.team_id, """
with store.lock(team_id):
    print('LOCKED', flush=True)
    sys.stdin.readline()
""")
    try:
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(TeamError, match="team_store_busy"):
            with store.lock(team.team_id, timeout=0.04):
                pytest.fail("不应取得另一个进程持有的锁")
        assert json.loads((paths.team(team.team_id) / "team.json").read_bytes())["name"] == "original"
        # 模拟进程崩溃，旧 owner 仍在但内核锁已释放，不借助 mtime。
        child.kill()
        child.communicate(timeout=5)
        with store.lock(team.team_id):
            pass
        assert (paths.team(team.team_id) / "locks" / "store.lock").read_bytes() == b"\0"
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def test_real_cross_process_cas_has_one_winner_and_one_conflict(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("original")
    body = """
team = store.load_team(team_id)
print('READY', flush=True)
sys.stdin.readline()
try:
    change = RecordChange('team', team_id, replace(team, name=sys.argv[5], revision=team.revision+1).to_dict())
    store.commit_transaction(team_id, expected_revision=team.revision, mutation_id=new_id('mutation'), changes=(change,))
    print('COMMITTED')
except TeamError as error:
    print(error.code)
"""
    children = [child_process(paths, team.team_id, body, name) for name in ("one", "two")]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "READY"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        outputs = [child.communicate(timeout=10) for child in children]
        assert sorted(stdout.strip() for stdout, _ in outputs) == ["COMMITTED", "team_revision_conflict"]
        assert all(not stderr for _, stderr in outputs)
        final = store.load_team(team.team_id)
        assert final.revision == 2 and final.name in {"one", "two"}
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


@pytest.mark.parametrize("stage", ["before_commit", "before_publish", "after_commit", "after_projection:team"])
def test_real_process_crash_replays_commit_without_reexecuting_business(tmp_path, stage):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("original")
    mutation = new_id("mutation")
    child = child_process(paths, team.team_id, """
team = store.load_team(team_id)
change = RecordChange('team', team_id, replace(team, name='durable', revision=team.revision+1).to_dict())
store._fault = lambda stage: os._exit(73) if stage == sys.argv[6] else None
store.commit_transaction(team_id, expected_revision=1, mutation_id=sys.argv[5], changes=(change,))
""", mutation, stage)
    _, stderr = child.communicate(timeout=10)
    assert child.returncode == 73 and not stderr
    restored = store.load_team(team.team_id)
    committed = stage not in {"before_commit", "before_publish"}
    assert (restored.name, restored.revision) == (("durable", 2) if committed else ("original", 1))
    if committed:
        result = store.commit_transaction(team.team_id, expected_revision=1, mutation_id=mutation, changes=(change_name(team, "durable"),))
        assert result.replayed and result.commit_sequence == 2


def test_projection_rebuilt_but_corrupt_journal_preserved_and_refused(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("original")
    projection = paths.team(team.team_id) / "team.json"
    projection.write_text("broken projection", encoding="utf-8")
    assert store.load_team(team.team_id) == team
    assert json.loads(projection.read_bytes())["name"] == "original"
    journal = next((paths.team(team.team_id) / "transactions").glob("*.json"))
    original = journal.read_bytes()
    journal.write_bytes(original[:-1] + b"!")
    corrupt = journal.read_bytes()
    before = projection.read_bytes()
    with pytest.raises(TeamError, match="team_store_corrupt"):
        store.load_team(team.team_id)
    assert journal.read_bytes() == corrupt and projection.read_bytes() == before


def test_unknown_lock_owner_refuses_without_overwriting_owner(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("original")
    lock = paths.team(team.team_id) / "locks" / "store.lock"
    lock.write_bytes(b"\0unknown owner")
    with pytest.raises(TeamError, match="team_store_busy"):
        store.load_team(team.team_id)
    assert lock.read_bytes() == b"\0unknown owner"


def test_actual_projection_junction_never_overwrites_outside(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("original")
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    # 用新投影目录模拟替换：校验在写入临时文件前拒绝。
    make_directory_link(paths.team(team.team_id) / "tasks", outside)
    with pytest.raises(TeamError):
        store.atomic_write((team.team_id, "tasks", "never.json"), b"unsafe")
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]
    assert marker.read_text(encoding="utf-8") == "keep"


def test_broadcast_commit_is_complete_redacted_and_sequences_restore(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths, sensitive_values=("unit-test-secret",))
    team = store.create_team("original")
    workers = [TeamMemberRecord(name, name, agent_name="reader") for name in ("alice", "bob")]
    store.commit_transaction(team.team_id, expected_revision=1, mutation_id=new_id("mutation"),
                             changes=tuple(RecordChange("members", m.member_id, m.to_dict()) for m in workers))
    broadcast = new_id("broadcast")
    messages = tuple(TeamMessage(new_id("message"), team.team_id, "lead", name, "unit-test-secret", utc_now(), index, "send", broadcast_id=broadcast)
                     for index, name in enumerate(("alice", "bob"), 1))
    with pytest.raises(TeamError, match="team_message_invalid"):
        store.commit_transaction(team.team_id, expected_revision=2, mutation_id=new_id("mutation"),
                                 changes=(RecordChange("messages", messages[0].message_id, messages[0].to_dict()),))
    store.commit_transaction(team.team_id, expected_revision=2, mutation_id=new_id("mutation"),
                             changes=tuple(RecordChange("messages", m.message_id, m.to_dict()) for m in messages))
    restored = store.load_records(team.team_id, "messages")
    assert sorted(m["sequence"] for m in restored) == [1, 2]
    assert {m["recipient"] for m in restored} == {"alice", "bob"}
    for path in paths.team(team.team_id).rglob("*.json"):
        assert "unit-test-secret" not in path.read_text(encoding="utf-8")


def test_log_limit_failure_preserves_existing_business_state(tmp_path, monkeypatch):
    import newcode.teams.store as module
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("original")
    monkeypatch.setattr(module, "MAX_TRANSACTIONS", 1)
    with pytest.raises(TeamError, match="team_limit_exceeded"):
        store.commit_transaction(team.team_id, expected_revision=1, mutation_id=new_id("mutation"), changes=(change_name(team),))
    assert store.load_team(team.team_id) == team


def test_exclusive_native_publish_and_team_id_collision_never_overwrite(tmp_path, monkeypatch):
    import newcode.teams.store as module
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("original")
    path = paths.team(team.team_id) / "team.json"
    before = path.read_bytes()
    with pytest.raises(TeamError):
        store.atomic_write((team.team_id, "team.json"), b"overwrite", replace=False)
    assert path.read_bytes() == before
    actual_new_id = module.new_id
    monkeypatch.setattr(module, "new_id", lambda kind: team.team_id if kind == "team" else actual_new_id(kind))
    with pytest.raises(TeamError, match="team_name_conflict"):
        store.create_team("collision")
    assert path.read_bytes() == before


def test_unsupported_durability_fails_closed_preserving_existing_commit(tmp_path, monkeypatch):
    import newcode.teams.store as module
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("original")
    before = (paths.team(team.team_id) / "team.json").read_bytes()
    def unsupported(*args, **kwargs):
        raise TeamError("team_store_unavailable")
    monkeypatch.setattr(module, "durable_publish", unsupported)
    with pytest.raises(TeamError, match="team_store_unavailable"):
        store.commit_transaction(team.team_id, expected_revision=1, mutation_id=new_id("mutation"), changes=(change_name(team),))
    assert (paths.team(team.team_id) / "team.json").read_bytes() == before
    assert len(list((paths.team(team.team_id) / "transactions").glob("*.json"))) == 1
    assert list((paths.team(team.team_id) / "transactions").glob(".*.tmp")) == []


def test_actual_crash_mid_broadcast_projection_restores_all_recipients_once(tmp_path):
    paths = make_paths(tmp_path)
    store = TeamStore(paths)
    team = store.create_team("broadcast")
    members = [TeamMemberRecord(name, name, agent_name="reader") for name in ("alice", "bob")]
    store.commit_transaction(team.team_id, expected_revision=1, mutation_id=new_id("mutation"),
                             changes=tuple(RecordChange("members", m.member_id, m.to_dict()) for m in members))
    broadcast = new_id("broadcast")
    messages = [TeamMessage(new_id("message"), team.team_id, "lead", name, "safe", utc_now(), sequence, "broadcast", broadcast_id=broadcast).to_dict()
                for sequence, name in enumerate(("alice", "bob"), 1)]
    mutation = new_id("mutation")
    child = child_process(paths, team.team_id, """
import json
changes = tuple(RecordChange('messages', m['message_id'], m) for m in json.loads(sys.argv[6]))
store._fault = lambda stage: os._exit(75) if stage == 'after_projection:messages' else None
store.commit_transaction(team_id, expected_revision=2, mutation_id=sys.argv[5], changes=changes)
""", mutation, json.dumps(messages))
    _, stderr = child.communicate(timeout=10)
    assert child.returncode == 75 and not stderr
    assert len(list((paths.team(team.team_id) / "messages").glob("*.json"))) == 1
    restored = store.load_records(team.team_id, "messages")
    assert {m["recipient"] for m in restored} == {"alice", "bob"}
    assert sorted(m["sequence"] for m in restored) == [1, 2]
    repeated = store.commit_transaction(team.team_id, expected_revision=2, mutation_id=mutation,
                                        changes=tuple(RecordChange("messages", m["message_id"], m) for m in messages))
    assert repeated.replayed and repeated.commit_sequence == 3
    assert len(store.load_records(team.team_id, "messages")) == 2
