"""T20：只读集成预览；本地 Git 与真实成员执行屏障。"""

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import os
import stat
import threading
from types import SimpleNamespace

import pytest

from newcode.permissions.types import PermissionDecisionValue
from newcode.teams.integration import TeamIntegrationManager
from newcode.teams.types import RecordChange, TeamError, new_id
from test_subagents_worktrees import _git
from test_teams_members import setup_member, member_record


@contextmanager
def lead_barrier():
    # 测试中 Lead 没有 AgentLoop/工具执行者；生产接入须提供真实屏障。
    yield


@pytest.fixture
def integration(tmp_path):
    runner, _, _ = setup_member(tmp_path)
    root = runner.store.paths.workspace_root
    _git(root, "branch", "-m", "delivery")
    (root / "remove.txt").write_text("remove me", encoding="utf-8")
    (root / ".gitignore").write_text(".newcode/worktrees/\nignored/\n", encoding="utf-8")
    _git(root, "add", "remove.txt", ".gitignore")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "preview base")
    runner.activate()
    service = TeamIntegrationManager(runner.store, runner.scope.team_id,
        worktrees=runner.worktrees, runners={"alice": runner}, role="lead",
        authorize=lambda arguments: PermissionDecisionValue.ALLOW, lead_barrier=lead_barrier)
    try:
        yield service, runner, root, runner.tool_context.cwd
    finally:
        runner.close()


def preview(service, runner):
    return service.preview("alice", member_record(runner).revision)


def tree_bytes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and p.suffix != ".lock"}


def test_preview_readonly_stable_staged_unstaged_delete_binary_and_rename(integration):
    service, runner, root, child = integration
    (child / "README.md").write_text("staged", encoding="utf-8")
    _git(child, "add", "README.md")
    (child / "README.md").write_text("final", encoding="utf-8")
    (child / "remove.txt").unlink()
    (child / "renamed.txt").write_text("remove me", encoding="utf-8")
    (child / "binary.dat").write_bytes(b"\x00\xff\x01")
    before = tree_bytes(root), tree_bytes(runner.store.paths.root)
    first = preview(service, runner)
    assert first == preview(service, runner)
    assert first.target_branch == "delivery" and first.base_commit == runner._lease.base_commit
    assert first.verification == "not_verified" and len(first.snapshot_digest) == 64
    entries = {entry.path: entry for entry in first.files}
    assert entries["README.md"].staged_object_id != entries["README.md"].base_object_id
    assert entries["README.md"].content == b"final"
    assert entries["remove.txt"].content is None
    assert entries["renamed.txt"].base_object_id is None
    assert entries["binary.dat"].binary and first.total_bytes < 1024
    assert before == (tree_bytes(root), tree_bytes(runner.store.paths.root))
    assert b"base contents" in (root / "README.md").read_bytes()
    assert not (runner.store.paths.team(runner.scope.team_id) / "integrations").exists()
    with pytest.raises(TeamError, match="team_coordinator_unavailable"):
        service.integrate()


@pytest.mark.parametrize("decision", [PermissionDecisionValue.DENY, PermissionDecisionValue.REQUIRE_CONFIRMATION])
def test_permission_fail_closed_before_git_or_store(integration, decision, monkeypatch):
    service, runner, root, child = integration
    service.authorize = lambda arguments: decision
    monkeypatch.setattr(service.git, "current_branch", lambda *_: pytest.fail("拒绝后不得查询 Git"))
    before = tree_bytes(root)
    with pytest.raises(TeamError, match="permission_denied"):
        preview(service, runner)
    assert before == tree_bytes(root)


def test_role_revision_busy_and_unproven_executor_refused(integration):
    service, runner, _, _ = integration
    service.role = "member"
    with pytest.raises(TeamError, match="team_role_denied"):
        preview(service, runner)
    service.role = "lead"
    with pytest.raises(TeamError, match="team_revision_conflict"):
        service.preview("alice", member_record(runner).revision + 1)
    runner._execution.acquire()
    try:
        with pytest.raises(TeamError, match="team_member_busy"):
            preview(service, runner)
    finally:
        runner._execution.release()
    scope = runner.scope
    runner.scope = replace(scope, generation=scope.generation + 1)
    try:
        with pytest.raises(TeamError, match="team_member_busy"):
            preview(service, runner)
    finally:
        runner.scope = scope
    service.runners = {}
    with pytest.raises(TeamError, match="team_member_busy"):
        preview(service, runner)


def test_dirty_target_ignored_collision_and_unknown_git_preserve(integration, monkeypatch):
    service, runner, root, child = integration
    (child / "ignored").mkdir()
    (child / "ignored" / "retained.txt").write_text("ignored source", encoding="utf-8")
    assert preview(service, runner).files == ()
    (root / "README.md").write_text("external dirty", encoding="utf-8")
    with pytest.raises(TeamError, match="team_target_dirty"):
        preview(service, runner)
    (root / "README.md").write_text("base contents\n", encoding="utf-8")
    (root / "ignored").mkdir()
    (root / "ignored" / "retained.txt").write_text("ignored target", encoding="utf-8")
    _git(child, "add", "-f", "ignored/retained.txt")
    before = tree_bytes(root)
    with pytest.raises(TeamError, match="team_snapshot_invalid"):
        preview(service, runner)
    assert before == tree_bytes(root)
    monkeypatch.setattr(service.git, "integration_index", lambda *_: (_ for _ in ()).throw(OSError("private error")))
    with pytest.raises(TeamError) as error:
        preview(service, runner)
    assert "private error" not in str(error.value)


@pytest.mark.parametrize("path,content", [(".env", b"A=private"), ("notes.txt", b"api_key=not-production"),
    ("runtime/session.json", b"fixture-secret"), ("big.bin", b"x" * (1024 * 1024 + 1)),
    (".gitattributes", b"*.txt filter=evil")], ids=["env", "credential", "known-secret", "oversize", "attributes"])
def test_unsafe_content_paths_and_limits_refused_without_changes(integration, path, content):
    service, runner, root, child = integration
    target = child / path
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(content)
    before = tree_bytes(root)
    with pytest.raises(TeamError, match="team_snapshot_invalid"):
        preview(service, runner)
    assert before == tree_bytes(root)


def test_base_owner_and_external_changes_fail_closed(integration, monkeypatch):
    import newcode.teams.integration as module
    service, runner, root, child = integration
    (child / "README.md").write_text("change", encoding="utf-8")
    original = module._read_file
    calls = []
    def changing(cwd, path, *args, **kwargs):
        result = original(cwd, path, *args, **kwargs)
        if cwd == child and path == "README.md" and not calls:
            calls.append(path)
            (child / "README.md").write_text("concurrent edit", encoding="utf-8")
        return result
    monkeypatch.setattr(module, "_read_file", changing)
    with pytest.raises(TeamError, match="team_snapshot_invalid|team_target_changed"):
        preview(service, runner)
    monkeypatch.setattr(module, "_read_file", original)
    member = member_record(runner)
    team = runner.store.load_team(runner.scope.team_id)
    runner.store.commit_transaction(team.team_id, expected_revision=team.revision, mutation_id=new_id("mutation"),
        changes=(RecordChange("members", "alice", replace(member, revision=member.revision + 1, base_commit="0" * 40).to_dict()),))
    with pytest.raises(TeamError, match="team_worktree_owner_invalid"):
        preview(service, runner)


def test_stage_only_no_code_changes_and_base_ancestor(integration):
    service, runner, root, child = integration
    (child / "README.md").write_text("stage only", encoding="utf-8")
    _git(child, "add", "README.md")
    (child / "README.md").write_text("base contents\n", encoding="utf-8")
    result = preview(service, runner)
    assert result.no_code_changes and result.files[0].staged_object_id != result.files[0].base_object_id
    assert result.files[0].content == (root / "README.md").read_bytes()


def test_native_store_lock_contention_retains_data(integration):
    service, runner, root, _ = integration
    revision = member_record(runner).revision
    ready, release = threading.Event(), threading.Event()
    def locked():
        with runner.store.lock(runner.scope.team_id):
            ready.set()
            release.wait(5)
    worker = threading.Thread(target=locked)
    worker.start()
    assert ready.wait(2)
    try:
        with pytest.raises(TeamError, match="team_store_busy"):
            service.preview("alice", revision)
    finally:
        release.set()
        worker.join(5)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/escape", "a\\b", "a//b", "a/../b", ":(top)x", "a:ads", "a/."])
def test_manifest_paths_reject_escape_without_open(path, monkeypatch):
    import newcode.teams.integration as module
    from newcode.worktrees.types import WorktreeError
    monkeypatch.setattr(module.os, "open", lambda *a, **k: pytest.fail("非法路径不得打开文件"))
    with pytest.raises((TeamError, WorktreeError)):
        module._read_file("unused", path, 8)


@pytest.mark.parametrize("mode,attrs,links", [(stat.S_IFLNK, 0, 1), (stat.S_IFREG, 0x400, 1),
    (stat.S_IFREG, 0, 2), (stat.S_IFDIR, 0, 1)])
def test_reparse_symlink_hardlink_special_rejected_before_read(mode, attrs, links, monkeypatch):
    import newcode.teams.integration as module
    monkeypatch.setattr(module.os, "open", lambda *a, **k: pytest.fail("不得读取链接目标"))
    with pytest.raises(TeamError, match="team_snapshot_invalid"):
        module._plain(SimpleNamespace(st_mode=mode, st_file_attributes=attrs, st_nlink=links))


@pytest.mark.parametrize("raw,index", [(b"120000 blob " + b"a" * 40 + b"\tlink\0", False),
    (b"160000 commit " + b"a" * 40 + b"\tsubmodule\0", False),
    (b"100644 " + b"a" * 40 + b" 1\tconflicted\0", True),
    (b"100644 " + b"a" * 40 + b" 0\t../escape\0", True), (b"truncated", False)],
    ids=["symlink", "submodule", "unmerged", "escape", "truncated"])
def test_git_inventory_invalid_modes_stages_paths(raw, index):
    from newcode.teams.integration import _entries
    from newcode.worktrees.types import WorktreeError
    with pytest.raises((TeamError, WorktreeError, ValueError)):
        _entries(raw, index=index)


def test_actual_hardlink_rejected_no_outside_read_or_write(tmp_path, monkeypatch):
    from newcode.teams.integration import _read_file
    outside = tmp_path / "outside.txt"
    outside.write_text("outside sentinel", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    os.link(outside, root / "linked.txt")
    with pytest.raises(TeamError):
        _read_file(root, "linked.txt", 1024)
    assert outside.read_text(encoding="utf-8") == "outside sentinel"


def test_real_inflight_tool_not_idle_or_cancel_flag(integration, monkeypatch):
    from newcode.providers.base import TextDelta, ToolCallEvent
    from newcode.tools.types import ToolCall
    service, runner, _, _ = integration
    revision = member_record(runner).revision
    entered, release = threading.Event(), threading.Event()
    inner = runner.registry.get("read_file")
    original = inner.run
    def blocked(arguments, context):
        entered.set()
        assert release.wait(20)
        return original(arguments, context)
    monkeypatch.setattr(inner, "run", blocked)
    calls = []
    def callback(*args):
        calls.append(1)
        if len(calls) == 1:
            yield ToolCallEvent([ToolCall("active-read", "read_file", {"path": "README.md"})])
        else:
            yield TextDelta("done")
    runner._provider.callback = callback
    errors = []
    def execute():
        try:
            runner.activate("read")
        except TeamError as exc:
            errors.append(exc.code)
    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert entered.wait(20)
        monkeypatch.setattr(service.git, "integration_tree", lambda *_: pytest.fail("活动执行者不应开始扫描"))
        with pytest.raises(TeamError, match="team_store_busy|team_member_busy|team_revision_conflict"):
            service.preview("alice", revision)
        runner.cancel.set()
        with pytest.raises(TeamError):
            service.preview("alice", revision)
        assert worker.is_alive()  # cancel 不是线程/执行器已经退出。
    finally:
        release.set()
        worker.join(20)
    assert not worker.is_alive()


def test_external_target_change_descendant_head_and_digest_binding(integration, monkeypatch):
    import newcode.teams.integration as module
    service, runner, root, child = integration
    (child / "README.md").write_text("member change", encoding="utf-8")
    first = preview(service, runner)
    (root / "other-member.txt").write_text("already integrated fixture", encoding="utf-8")
    _git(root, "add", "other-member.txt")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "other member")
    second = preview(service, runner)
    assert first.base_commit == second.base_commit and first.target_head != second.target_head
    assert first.snapshot_digest != second.snapshot_digest
    assert {f.path for f in second.files} == {"README.md"}
    original = module._read_file
    changed = []
    def concurrent(cwd, path, *args, **kwargs):
        result = original(cwd, path, *args, **kwargs)
        if cwd == child and path == "README.md" and not changed:
            changed.append(1)
            (root / "README.md").write_text("external target edit", encoding="utf-8")
        return result
    monkeypatch.setattr(module, "_read_file", concurrent)
    with pytest.raises(TeamError, match="team_target_changed|team_target_dirty"):
        preview(service, runner)
    assert (root / "README.md").read_text(encoding="utf-8") == "external target edit"


def test_external_driver_config_rejected_before_status_no_execution(integration, monkeypatch):
    service, runner, root, child = integration
    sentinel = root / "driver-ran"
    _git(root, "config", "filter.unsafe.clean", "not-a-program")
    (child / ".gitattributes").write_text("*.md filter=unsafe", encoding="utf-8")
    monkeypatch.setattr(service.git, "status", lambda *_: pytest.fail("驱动检查须在 status 前"))
    with pytest.raises(TeamError, match="team_snapshot_invalid"):
        preview(service, runner)
    assert not sentinel.exists()


def test_snapshot_total_limit_and_index_link_rejection(integration, monkeypatch):
    import newcode.teams.integration as module
    service, runner, root, child = integration
    budget = sum((root / p).stat().st_size for p in ("README.md", ".gitignore", "remove.txt")) + 8
    (child / "one.txt").write_bytes(b"a" * (budget // 2 + 1))
    (child / "two.txt").write_bytes(b"b" * (budget // 2 + 1))
    # 仅压低测试预算，不放宽生产 16 MiB/1 MiB 上限。
    monkeypatch.setattr(module, "MAX_SNAPSHOT_BYTES", budget)
    with pytest.raises(TeamError, match="team_snapshot_invalid"):
        preview(service, runner)
    monkeypatch.undo()
    index = service.git.integration_index_path(child)
    outside = root.parent / "outside-index"
    outside.write_bytes(index.read_bytes())
    index.unlink()
    os.link(outside, index)
    before = outside.read_bytes()
    with pytest.raises(TeamError):
        preview(service, runner)
    assert outside.read_bytes() == before


def test_preview_does_not_repair_missing_projection(integration):
    service, runner, _, _ = integration
    revision = member_record(runner).revision
    path = runner.store.paths.checked(runner.scope.team_id, "members", "alice.json")
    path.unlink()  # 有效提交日志仍为事实；preview 不修复投影。
    result = service.preview("alice", revision)
    assert result.no_code_changes and not path.exists()


def test_manager_queries_are_readonly_fixed_argv_and_hooks_disabled(tmp_path):
    from test_worktrees_git import FakeRunner, _adapter
    raw = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    executor = FakeRunner(raw)
    adapter, root = _adapter(tmp_path, executor)
    oid = "a" * 40
    adapter.integration_tree(root, oid)
    adapter.integration_index(root)
    adapter.integration_untracked(root)
    adapter.integration_changes(root, oid)
    adapter.integration_config(root)
    adapter.integration_attributes(root, ["a file.txt", "semi;colon.txt", "quote'file.txt"])
    adapter.integration_ignored_changes(root, ["a file.txt", "semi;colon.txt"])
    assert adapter.integration_ancestor(root, oid, oid)
    for argv, options in executor.calls:
        command = argv[argv.index("-C") + 2:]
        assert command[0] in {"ls-tree", "ls-files", "diff", "config", "check-attr", "merge-base", "check-ignore"}
        assert "core.hooksPath=" in argv[2] and options["shell"] is False
        assert options["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert options["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert options["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    before = len(executor.calls)
    with pytest.raises(ValueError):
        adapter.integration_tree(root, "--malicious-option")
    with pytest.raises(ValueError):
        adapter.integration_attributes(root, ["../outside"])
    assert len(executor.calls) == before


def test_forced_ignored_and_external_git_lock_rejected(integration):
    service, runner, root, child = integration
    (child / "ignored").mkdir()
    (child / "ignored" / "local.txt").write_text("keep ignored", encoding="utf-8")
    _git(child, "add", "-f", "ignored/local.txt")
    with pytest.raises(TeamError, match="team_snapshot_invalid"):
        preview(service, runner)
    assert (child / "ignored" / "local.txt").read_text() == "keep ignored"
    lock = root / ".git" / "index.lock"
    lock.write_bytes(b"external owner")
    with pytest.raises(TeamError, match="team_target_changed"):
        preview(service, runner)
    assert lock.read_bytes() == b"external owner"


def test_unsafe_attributes_missing_base_and_branch_binding(integration, monkeypatch):
    service, runner, root, child = integration
    from newcode.worktrees.types import WorktreeError, WorktreeErrorCode
    original = service.git.integration_attributes
    monkeypatch.setattr(service.git, "integration_attributes", lambda *a: b"README.md\0filter\0evil\0")
    with pytest.raises(TeamError, match="team_snapshot_invalid"):
        preview(service, runner)
    monkeypatch.setattr(service.git, "integration_attributes", original)
    first = preview(service, runner)
    _git(root, "branch", "-m", "delivery-v2")
    second = preview(service, runner)
    assert first.target_head == second.target_head and first.snapshot_digest != second.snapshot_digest
    original_tree = service.git.integration_tree
    monkeypatch.setattr(service.git, "integration_tree", lambda *a: (_ for _ in ()).throw(WorktreeError(WorktreeErrorCode.GIT_FAILED)))
    with pytest.raises(TeamError, match="team_snapshot_invalid"):
        preview(service, runner)
    monkeypatch.setattr(service.git, "integration_tree", original_tree)
    _git(root, "branch", "-m", "fixture-secret")
    result = preview(service, runner)
    assert "fixture-secret" not in str(result.summary()) and "fixture-secret" not in repr(result)


def test_reparse_file_fixture_cannot_read_outside(tmp_path, monkeypatch):
    import newcode.teams.integration as module
    from pathlib import Path
    root = tmp_path / "root"
    root.mkdir()
    candidate = root / "link"
    candidate.write_bytes(b"fixture reparse target")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside unchanged")
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == candidate:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400, st_nlink=1)
        return info
    monkeypatch.setattr(Path, "lstat", lstat)
    opened = []
    monkeypatch.setattr(module.os, "open", lambda *a, **k: opened.append(a) or pytest.fail("reparse 文件不得读取"))
    with pytest.raises(TeamError):
        module._read_file(root, "link", 1024)
    assert not opened and outside.read_bytes() == b"outside unchanged"


@pytest.mark.skipif(os.name != "nt", reason="Windows 原生 no-share-delete 目录句柄验证")
def test_actual_windows_directory_guard_blocks_replacement(tmp_path):
    from newcode.teams.integration import _directory_guard
    root = tmp_path / "root"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    target = tmp_path / "moved"
    with _directory_guard(child, boundary=root):
        with pytest.raises(OSError) as error:
            child.rename(target)
        assert error.value.winerror == 32
        assert child.is_dir() and not target.exists()
    child.rename(target)
    assert target.is_dir()


def test_target_hash_streaming_not_subject_to_snapshot_file_limit(tmp_path):
    from newcode.teams.integration import _directory_guard, _read_file_locked
    data = b"a" * (2 * 1024 * 1024)
    (tmp_path / "large-existing.bin").write_bytes(data)
    with _directory_guard(tmp_path, boundary=tmp_path):
        digest, identity, _ = _read_file_locked(tmp_path, "large-existing.bin", None, hash_only=True)
    assert digest == hashlib.sha256(data).hexdigest() and identity[2] == len(data)


def integration_record(team_id, **overrides):
    from newcode.teams.types import IntegrationRecord
    values = dict(integration_id="integration-" + "a" * 32, team_id=team_id, member_id="alice",
        member_revision=1, generation=1, base_commit="b" * 40, target_head="c" * 40,
        target_branch="delivery", snapshot_digest="d" * 64, idempotency_key="request-1",
        target_signature="e" * 64, manifest=())
    return IntegrationRecord(**(values | overrides))


def test_t21_integration_record_strict_and_old_journal_replay(tmp_path):
    from test_teams_paths import make_paths
    from newcode.teams.store import TeamStore
    from newcode.teams.types import IntegrationRecord, TeamMemberRecord
    store = TeamStore(make_paths(tmp_path))
    team = store.create_team("old log")
    member = TeamMemberRecord("alice", "alice", agent_name="worker", base_commit="b" * 40,
                              runtime_generation=1, auto_wake=False)
    store.commit_transaction(team.team_id, expected_revision=team.revision, mutation_id=new_id("mutation"),
        changes=(RecordChange("members", "alice", member.to_dict()),))
    assert store.load_records(team.team_id, "integrations") == ()
    record = integration_record(team.team_id)
    team = store.load_team(team.team_id)
    store.commit_transaction(team.team_id, expected_revision=team.revision, mutation_id=new_id("mutation"),
        changes=(RecordChange("integrations", record.integration_id, record.to_dict()),
                 RecordChange("members", "alice", replace(member, revision=2).to_dict())))
    projection = store.paths.checked(team.team_id, "integrations", record.integration_id + ".json")
    projection.unlink()
    assert IntegrationRecord.from_dict(store.load_records(team.team_id, "integrations")[0]) == record
    assert projection.exists()
    for changes in ({"version": 2}, {"member_id": "../escape"}, {"stage": "completed"},
                    {"generation": 0}, {"target_branch": "--option"}, {"base_commit": "HEAD"},
                    {"source_commit": "f" * 40}, {"stage": []}):
        with pytest.raises(TeamError):
            IntegrationRecord.from_dict(record.to_dict() | changes)
    before = tree_bytes(store.paths.root)
    team = store.load_team(team.team_id)
    invalid = replace(record, revision=2, stage="applying_target", source_commit="f" * 40,
                      source_tree="1" * 40, merge_tree="2" * 40, integrated_commit="3" * 40)
    with pytest.raises(TeamError):
        store.commit_transaction(team.team_id, expected_revision=team.revision, mutation_id=new_id("mutation"),
            changes=(RecordChange("integrations", record.integration_id, invalid.to_dict()),))
    assert tree_bytes(store.paths.root) == before
    # 即使攻击者重算外层 checksum，未知 integration 版本也不得重放为 Git 意图。
    import json
    from newcode.teams.store import encode
    journal = sorted(store.paths.checked(team.team_id, "transactions").glob("*.json"))[-1]
    payload = json.loads(journal.read_bytes())
    payload.pop("digest")
    for change in payload["changes"]:
        if change["collection"] == "integrations":
            change["value"]["version"] = 2
    payload["digest"] = hashlib.sha256(encode(payload)).hexdigest()
    journal.write_bytes(encode(payload))
    manager = TeamIntegrationManager(store, team.team_id, worktrees=SimpleNamespace(_git=object()),
        runners={}, role="lead", authorize=lambda args: PermissionDecisionValue.ALLOW, lead_barrier=lead_barrier)
    with pytest.raises(TeamError, match="team_store_corrupt"):
        manager.integrate(target_member_id="alice", expected_member_revision=1,
            expected_target_head="c" * 40, snapshot_digest="d" * 64, idempotency_key="request-1", confirmation=True)


def test_t21_success_binary_delete_and_idempotent_response_loss(integration):
    service, runner, root, child = integration
    hook_marker = root.parent / "hook-must-not-run"
    for name in ("post-checkout", "pre-commit", "post-commit", "pre-merge-commit", "post-merge"):
        hook = root / ".git" / "hooks" / name
        hook.write_text("#!/bin/sh\nprintf unsafe > '" + hook_marker.as_posix() + "'\nexit 91\n", encoding="utf-8")
        hook.chmod(0o755)
    (child / "README.md").write_bytes(b"integrated\n")
    (child / "binary.dat").write_bytes(b"\0\xff")
    (child / "remove.txt").unlink()
    request = preview(service, runner)
    service.confirm_single_writer = lambda bound: True
    args = dict(target_member_id="alice", expected_member_revision=member_record(runner).revision,
        expected_target_head=request.target_head, snapshot_digest=request.snapshot_digest,
        idempotency_key="one", confirmation=True)
    try:
        result = service.integrate(**args)
    except TeamError as error:
        if error.code == "team_integration_pending" and error.__context__ is not None:
            raise error.__context__
        raise
    assert result["stage"] == "completed" and result["verification"] == "not_verified"
    assert not hook_marker.exists()
    assert (root / "README.md").read_bytes() == b"integrated\n"
    assert (root / "binary.dat").read_bytes() == b"\0\xff" and not (root / "remove.txt").exists()
    assert service.git.head(root) == result["I"] and service.git.status(root) == b""
    assert member_record(runner).state.value == "integrated"
    before = tree_bytes(root)
    assert service.integrate(**args) == result
    assert tree_bytes(root) == before
    with pytest.raises(TeamError):
        runner.activate("不得从旧 Worktree 继续")
    with pytest.raises(TeamError):
        runner.wake(lambda scope: pytest.fail("integrated 不得排队"))
    with pytest.raises(TeamError):
        runner.resume()
    # 状态不是静止证明；执行锁仍被持有时不得释放任何 owner。
    owner, lease = runner._owner, runner._lease
    runner._execution.acquire()
    try:
        assert runner.close() is False
        assert runner._owner is owner and runner._lease is lease
    finally:
        runner._execution.release()
    assert runner.close() is True
    assert runner._owner is None and runner._lease is None
    assert service.integrate(**args) == result
    assert runner.close() is True
    assert member_record(runner).state.value == "integrated"


def test_t21_close_stale_generation_and_cleanup_failure(integration, monkeypatch):
    _, runner, _, _ = integration
    owner, lease, scope = runner._owner, runner._lease, runner.scope
    foreign = SimpleNamespace(__exit__=lambda *a: pytest.fail("不得释放新 owner"))
    runner._owner = foreign
    assert runner.close() is False and runner._lease is lease and runner._owner is foreign
    runner._owner = owner
    runner.scope = replace(scope, generation=scope.generation + 1)
    assert runner.close() is False
    assert runner._owner is owner and runner._lease is lease and owner.fd is not None
    runner.scope = scope
    # 一项失败仍释放其余安全资源；重试只处理上次失败项。
    context = SimpleNamespace(cleanup=lambda: (_ for _ in ()).throw(RuntimeError("fixture failure")))
    runner.context = context
    assert runner.close() is False
    assert runner._owner is None and runner._lease is None and runner._provider is None
    assert runner.context is context
    monkeypatch.setattr(context, "cleanup", lambda: None)
    assert runner.close() is True and runner.context is None
    from newcode.teams.runtime import StoreExecutorLeases
    leases = StoreExecutorLeases(runner.store)
    new_owner = leases.acquire(replace(scope, generation=scope.generation + 1))
    try:
        assert runner.close() is True
        with pytest.raises(TeamError, match="team_member_busy"):
            leases.acquire(replace(scope, generation=scope.generation + 2))
    finally:
        new_owner.__exit__(None, None, None)


def integration_args(service, runner, *, key="request"):
    request = preview(service, runner)
    service.confirm_single_writer = lambda bound: True
    return dict(target_member_id="alice", expected_member_revision=member_record(runner).revision,
        expected_target_head=request.target_head, snapshot_digest=request.snapshot_digest,
        idempotency_key=key, confirmation=True)


@pytest.mark.parametrize("point,expected", [
    ("before_source_stage", "team_integration_outcome_unknown"),
    ("before_journal_source_preparing", "team_integration_outcome_unknown"),
    ("after_journal_source_preparing", "completed"),
    ("after_source_commit", "completed"),
    ("after_journal_source_committed", "completed"),
    ("after_journal_merge_preparing", "completed"),
    ("after_merge", "team_integration_outcome_unknown"),
    ("after_merge_commit", "completed"),
    ("after_journal_merged", "completed"),
    ("before_journal_completed", "completed"),
    ("before_target_ff", "team_single_writer_required"),
    ("after_target_ff", "completed"),
    ("after_journal_completed", "completed"),
])
def test_t21_crash_recovery_matrix(integration, monkeypatch, point, expected):
    service, runner, root, child = integration
    (child / "README.md").write_bytes(b"frozen change\n")
    args = integration_args(service, runner)
    hit = []
    def fault(current):
        if current == point and not hit:
            hit.append(current)
            raise TimeoutError("fixture lost response")
    service.fault = fault
    with pytest.raises(Exception):
        service.integrate(**args)
    assert hit == [point]
    service.fault = lambda _: None
    # 记录中的 S/I 已存在时不得重做 commit。
    records = runner.store.load_records(runner.scope.team_id, "integrations")
    saved = records[0]
    commits = []
    original_commit = service.git._integration_commit
    monkeypatch.setattr(service.git, "_integration_commit", lambda *a: commits.append(a[-1]) or original_commit(*a))
    if expected != "completed":
        with pytest.raises(TeamError, match=expected):
            service.integrate(**args)
        if expected == "team_single_writer_required":
            assert service.integrate(**args)["stage"] == "completed"
        else:
            before = tree_bytes(root)
            with pytest.raises(TeamError, match=expected):
                service.integrate(**args)
            assert tree_bytes(root) == before
            return
    else:
        result = service.integrate(**args)
        assert result["stage"] == "completed" and service.git.head(root) == result["I"]
        assert (root / "README.md").read_bytes() == b"frozen change\n"
    if saved["source_commit"] is not None or point == "after_source_commit":
        assert "source" not in commits
    if saved["integrated_commit"] is not None or point == "after_merge_commit":
        assert "merge" not in commits


@pytest.mark.parametrize("coordinator_enabled", [False, True], ids=["lead", "coordinator"])
def test_t21_real_tool_permission_manager_and_local_git(integration, monkeypatch, coordinator_enabled):
    import json
    from newcode import cli
    from io import StringIO
    from pathlib import Path
    from newcode.permissions.manager import PermissionManager
    from newcode.permissions.types import PermissionMode
    from newcode.providers.base import ToolCallEvent, TextDelta
    from newcode.session import ChatSession
    from newcode.teams.config import TeamConfig, TeamConfigResult
    from newcode.teams.coordinator import CoordinatorPolicy
    from newcode.teams.runtime import TeamAttachment
    from newcode.tools.types import ToolCall, ToolContext
    service, runner, root, child = integration
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: runner.store.paths.home))
    (child / "README.md").write_bytes(b"tool integrated\n")
    team_config = TeamConfig(enabled=True, coordinator_enabled=coordinator_enabled)
    runtime = TeamAttachment(store=runner.store, config=team_config,
        coordinator=CoordinatorPolicy(coordinator_enabled),
        integration_worktrees=runner.worktrees, lead_barrier=lead_barrier,
        single_writer_confirm=lambda bound: True)
    backend = SimpleNamespace(request_stop=lambda *a: None, close=lambda **k: None, join_until=lambda *a: None)
    runtime._handles["alice"] = (runner, backend)
    monkeypatch.setattr(cli, "load_team_config", lambda *a, **k: TeamConfigResult(team_config))
    monkeypatch.setattr(cli, "load_hook_rules", lambda *a, **k: SimpleNamespace(rules=(), diagnostics=()))
    if coordinator_enabled:
        monkeypatch.setenv("NEWCODE_TEAM_COORDINATOR", "1")
    else:
        monkeypatch.delenv("NEWCODE_TEAM_COORDINATOR", raising=False)
    from newcode.teams.integration import TeamIntegrationManager
    constructed = []
    original_init = TeamIntegrationManager.__init__
    def observed_manager(self, *args, **kwargs):
        constructed.append(type(self))
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(TeamIntegrationManager, "__init__", observed_manager)
    revision = member_record(runner).revision
    class Provider:
        def stream_chat(self, messages, tools=None, allow_tool_calls=True):
            results = [json.loads(m.content) for m in messages if m.role == "tool"]
            if not results:
                yield ToolCallEvent([ToolCall("preview", "team_integrate", dict(operation="preview",
                    target_member_id="alice", expected_member_revision=revision))])
            elif len(results) == 1:
                assert results[0]["ok"], results[0]
                data = results[0]["data"]
                yield ToolCallEvent([ToolCall("integrate", "team_integrate", dict(operation="integrate",
                    target_member_id="alice", expected_member_revision=revision, expected_target_head=data["target_head"],
                    snapshot_digest=data["snapshot_digest"], idempotency_key="tool", confirmation=True))])
            else:
                yield TextDelta("完成")
    session = ChatSession()
    inputs = iter(["/team open " + runner.scope.team_id, "集成", "/exit"])
    assert cli.run_conversation(Provider(), session, tool_context=ToolContext(root),
        input_func=lambda _: next(inputs), output=StringIO(), error_output=StringIO(),
        agent_user_home=runner.store.paths.home, worktree_manager=runner.worktrees,
        permission_manager=PermissionManager(mode=PermissionMode.TRUSTED), team_attachment=runtime) == 0
    results = [json.loads(m.content) for m in session.messages if m.role == "tool"]
    assert len(results) == 2 and all(r["ok"] for r in results), results
    assert results[1]["data"]["stage"] == "completed" and (root / "README.md").read_bytes() == b"tool integrated\n"
    assert constructed == [TeamIntegrationManager, TeamIntegrationManager]
    assert runner.close() is True


def test_t21_barrier_tracks_actual_execution_and_http_timeout():
    from newcode.teams.integration import IntegrationBarrier
    barrier = IntegrationBarrier()
    entered, finish = threading.Event(), threading.Event()
    thread = threading.Thread(target=barrier.wrap(lambda: (entered.set(), finish.wait(2))))
    thread.start()
    try:
        assert entered.wait(1)
        with pytest.raises(TeamError, match="team_member_busy"):
            with barrier.hold():
                pytest.fail("运行中的 callback 不得集成")
    finally:
        finish.set()
        thread.join(2)
    barrier.http_transport = SimpleNamespace(_active_request=threading.Lock())
    barrier.http_transport._active_request.acquire()
    with pytest.raises(TeamError, match="team_member_busy"):
        with barrier.hold():
            pytest.fail("HTTP 超时后实际线程仍占用")
    barrier.http_transport._active_request.release()
    with barrier.hold():
        with pytest.raises(TeamError):
            barrier.wrap(lambda: pytest.fail("冻结期间不得开始新工作"))()
    assert barrier.wrap(lambda: 1)() == 1


def test_t21_single_writer_confirmation_is_once_not_permission_grant(tmp_path):
    from newcode.cli import _confirm_team_integration
    from newcode.permissions.manager import PermissionManager
    from newcode.permissions.types import ConfirmationResult, ConfirmationScope
    from newcode.tools.types import ToolContext
    calls = []
    manager = PermissionManager(confirmer=SimpleNamespace(confirm=lambda req, dec:
        calls.append((req, dec)) or ConfirmationResult(True, ConfirmationScope.ONCE)))
    bound = dict(integration_id="integration-" + "a" * 32, B="b" * 40, T="delivery", M="c" * 40, snapshot_digest="d" * 64)
    assert _confirm_team_integration(manager, ToolContext(tmp_path), bound) is True
    assert calls[0][0].original_args == bound and not manager.session_rules.rules
    manager.confirmer = SimpleNamespace(confirm=lambda *a: ConfirmationResult(True, ConfirmationScope.SESSION))
    assert _confirm_team_integration(manager, ToolContext(tmp_path), bound) is False


@pytest.mark.parametrize("gate", ["plan", "confirmation", "deny", "skill"])
@pytest.mark.parametrize("coordinator_enabled", [False, True], ids=["lead", "coordinator"])
def test_t21_tool_gate_zero_integration_manager(tmp_path, monkeypatch, gate, coordinator_enabled):
    import json
    from test_agent_loop_teams import make_loop
    from newcode.agent.mode import AgentMode
    from newcode.permissions.manager import PermissionManager
    from newcode.permissions.rules import parse_permission_rules_document
    from newcode.permissions.types import PermissionMode, PermissionLayer
    from newcode.teams.coordinator import CoordinatorPolicy
    from newcode.tools.types import ToolCall
    args = dict(operation="preview", target_member_id="alice", expected_member_revision=1)
    rules = parse_permission_rules_document({"rules": [{"id": "deny", "tool": "team_integrate", "action": "deny",
        "reason": "fixture", "risk_level": "high", "match": {"command_glob": "team_integrate *"}}]},
        source=PermissionLayer.PROJECT_RULES)
    permission = PermissionManager(mode=PermissionMode.DEFAULT) if gate == "confirmation" else PermissionManager(
        mode=PermissionMode.TRUSTED, **({"project_rules": rules} if gate == "deny" else {}))
    loop, runtime, _ = make_loop(tmp_path, [[ToolCall("preview", "team_integrate", args)]],
        permission=permission, coordinator=CoordinatorPolicy(coordinator_enabled))
    runtime.config = replace(runtime.config, coordinator_enabled=coordinator_enabled)
    runtime.team_id = runtime.store.create_team("gate").team_id
    runtime.integration_worktrees = object()
    runtime.lead_barrier = lead_barrier
    if gate == "skill":
        from test_skills_tool import _skill
        from newcode.skills.discovery import SkillDiscovery
        from newcode.skills.loader import SkillLoader
        _skill(loop.tool_context.workspace_root)
        catalog = SkillDiscovery().discover(loop.tool_context.workspace_root)
        loop.skill_state.activate(SkillLoader().load("demo", {}, catalog, available_tools=loop.registry.names()))
    monkeypatch.setattr(TeamIntegrationManager, "__init__", lambda *a, **k: pytest.fail("gate 拒绝后不得创建 Manager"))
    before = tree_bytes(runtime.store.paths.root)
    list(loop.run("预览", mode=AgentMode.PLAN if gate == "plan" else AgentMode.DO))
    results = [json.loads(m.content) for m in loop.session.messages if m.role == "tool"]
    assert len(results) == 1 and not results[0]["ok"]
    assert results[0]["error"]["code"] in {"permission_denied", "disallowed_tool", "team_coordinator_unavailable"}
    assert tree_bytes(runtime.store.paths.root) == before


@pytest.mark.parametrize("conflict", [False, True])
def test_t21_two_members_same_base_keep_previous_result(integration, conflict):
    from newcode.teams.member import TeamMemberRunner
    from newcode.teams.types import TeamMemberRecord, TeamPolicySnapshot, TeamScope
    service, alice, root, child = integration
    team = alice.store.load_team(alice.scope.team_id)
    bob_record = TeamMemberRecord("bob", "bob", agent_name="worker", auto_wake=True)
    alice.store.commit_transaction(team.team_id, expected_revision=team.revision, mutation_id=new_id("mutation"),
        changes=(RecordChange("members", "bob", bob_record.to_dict()),))
    scope = TeamScope(team.team_id, "bob", "20261016-101011-bb12", 1)
    bob = TeamMemberRunner(store=alice.store, scope=scope, definition=alice.definition, registry=alice.registry,
        provider_factory=alice.factory, default_model="fixture", worktree_manager=alice.worktrees,
        policy=TeamPolicySnapshot(scope, 1, frozenset(alice.registry.names()), frozenset(alice.registry.names())))
    try:
        bob.activate("第二成员")
        service.runners["bob"] = bob
        assert bob._lease.base_commit == alice._lease.base_commit
        (child / "README.md").write_bytes(b"first member\n")
        (bob.tool_context.cwd / ("README.md" if conflict else "second.txt")).write_bytes(b"second member\n")
        first = service.integrate(**integration_args(service, alice, key="first"))
        assert alice.close() is True
        record = next(m for m in alice.store.load_team(team.team_id).roster if m.member_id == "bob")
        second = service.preview("bob", record.revision)
        assert second.base_commit == first["B"] and second.target_head == first["I"]
        args = dict(target_member_id="bob", expected_member_revision=record.revision,
            expected_target_head=second.target_head, snapshot_digest=second.snapshot_digest,
            idempotency_key="second", confirmation=True)
        if conflict:
            with pytest.raises(TeamError, match="team_integration_conflict"):
                service.integrate(**args)
            assert service.git.head(root) == first["I"]
            assert (root / "README.md").read_bytes() == b"first member\n"
            before = tree_bytes(root)
            with pytest.raises(TeamError, match="team_integration_conflict"):
                service.integrate(**args)
            assert tree_bytes(root) == before
        else:
            result = service.integrate(**args)
            assert result["M"] == first["I"] and result["B"] == first["B"]
            assert (root / "README.md").read_bytes() == b"first member\n"
            assert (root / "second.txt").read_bytes() == b"second member\n"
            assert service.git.head(root) == result["I"] and service.git.status(root) == b""
    finally:
        assert bob.close() is True


@pytest.mark.parametrize("change", ["worktree", "index", "head"])
def test_t21_external_target_change_is_unknown_and_preserved(integration, change):
    service, runner, root, child = integration
    (child / "README.md").write_bytes(b"member\n")
    args = integration_args(service, runner)
    hit = []
    def fault(point):
        if point == "before_target_ff" and not hit:
            hit.append(point)
            (root / "README.md").write_bytes(b"external writer\n")
            if change in {"index", "head"}:
                _git(root, "add", "README.md")
            if change == "head":
                _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-m", "external change")
    service.fault = fault
    with pytest.raises(TeamError, match="team_integration_outcome_unknown"):
        service.integrate(**args)
    assert (root / "README.md").read_bytes() == b"external writer\n"
    assert runner.store.load_records(runner.scope.team_id, "integrations")[0]["stage"] == "outcome_unknown"
    before = tree_bytes(root)
    with pytest.raises(TeamError):
        service.integrate(**args)
    assert tree_bytes(root) == before
