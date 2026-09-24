from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from newcode.worktrees.git import WorktreeGitAdapter
from newcode.worktrees.manager import WorktreeManager, _FileLock, _TRANSITIONS
from newcode.worktrees.types import WorktreeError, WorktreeErrorCode, WorktreeRequest, WorktreeState


def _git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True):
    safe_env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if env:
        safe_env.update(env)
    return subprocess.run(
        ["git", *map(str, args)], cwd=str(cwd) if cwd else None, env=safe_env,
        shell=False, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _repo(tmp_path: Path, *, hooks: bool = False) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", str(repo))
    (repo / ".gitignore").write_text(".newcode/worktrees/\n", encoding="utf-8")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git("add", ".gitignore", "README.md", cwd=repo)
    _git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "initial", cwd=repo)

    local_hooks = repo / ".git" / "hooks"
    local_hooks.mkdir(exist_ok=True)
    local_sentinel = tmp_path / "local-hook-ran"
    script = f"#!/bin/sh\nprintf ran >> '{local_sentinel.as_posix()}'\n"
    post_checkout = local_hooks / "post-checkout"
    post_checkout.write_text(script, encoding="utf-8")
    post_checkout.chmod(0o700)
    _git("config", "core.hooksPath", str(local_hooks), cwd=repo)

    global_hooks = tmp_path / "global-hooks"
    global_hooks.mkdir()
    global_sentinel = tmp_path / "global-hook-ran"
    global_hook = global_hooks / "post-checkout"
    global_hook.write_text(f"#!/bin/sh\nprintf ran >> '{global_sentinel.as_posix()}'\n", encoding="utf-8")
    global_hook.chmod(0o700)
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(f"[core]\n\thooksPath = {global_hooks.as_posix()}\n", encoding="utf-8")
    return repo, local_sentinel, global_sentinel


def _manager(tmp_path: Path, *, base_env: dict[str, str] | None = None) -> WorktreeManager:
    hooks = tmp_path / "manager-empty-hooks"
    hooks.mkdir(exist_ok=True)
    git = WorktreeGitAdapter(
        empty_hooks_dir=hooks,
        executable=shutil.which("git") or "git",
        base_env=base_env or {"PATH": os.environ.get("PATH", "")},
    )
    return WorktreeManager(empty_hooks_dir=hooks, git=git)


def _owner_marker(lease: WorktreeLease) -> Path:
    import hashlib
    return lease.common_git_dir / "newcode-worktrees" / "owners" / f"{hashlib.sha256(lease.task_id.encode()).hexdigest()}.json"


def _age_marker(lease: WorktreeLease, age_seconds: float) -> None:
    stamp = time.time() - age_seconds
    os.utime(_owner_marker(lease), (stamp, stamp))


def _set_local_upstream(repo: Path, lease: WorktreeLease) -> None:
    _git("-C", str(repo), "branch", "--set-upstream-to=main", lease.branch)


def test_create_and_verify_requires_current_manager_lease_and_complete_identity(tmp_path: Path):
    repo, local_hook, global_hook = _repo(tmp_path, hooks=True)
    global_config = tmp_path / "global.gitconfig"
    manager = _manager(tmp_path, base_env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_GLOBAL": str(global_config)})
    lease = manager.create(WorktreeRequest(repo, "review", "task-001"))

    assert lease.state is WorktreeState.READY
    assert lease.path.is_dir()
    assert lease.path.resolve() == lease.path
    assert lease.branch == "newcode/subagent/task-001"
    assert manager.verify(lease) == lease
    assert not local_hook.exists()
    assert not global_hook.exists()
    assert len([item for item in manager.leases() if item.task_id == lease.task_id]) == 1

    # 新 Manager 没有内存 lease，即使 marker/path 存在也不能作为恢复依据。
    other_manager = _manager(tmp_path)
    with pytest.raises(WorktreeError) as exc:
        other_manager.verify(lease)
    assert exc.value.code == WorktreeErrorCode.LEASE_INVALID


def test_two_worktrees_get_unique_path_branch_and_registration(tmp_path: Path):
    repo, _, _ = _repo(tmp_path)
    manager = _manager(tmp_path)
    first = manager.create(WorktreeRequest(repo, "review", "task-001"))
    second = manager.create(WorktreeRequest(repo, "review", "task-002"))
    assert first.path != second.path
    assert first.branch != second.branch
    assert manager.verify(first) == first
    assert manager.verify(second) == second
    assert len(manager._git.list_worktrees(repo)) == 3  # main + 两个 child


def test_preexisting_directory_or_branch_is_never_reused_or_modified(tmp_path: Path):
    repo, _, _ = _repo(tmp_path)
    target = repo / ".newcode" / "worktrees" / "agent" / "task-001"
    target.mkdir(parents=True)
    sentinel = target / "keep.txt"
    sentinel.write_text("user data", encoding="utf-8")
    manager = _manager(tmp_path)
    with pytest.raises(WorktreeError) as exc:
        manager.create(WorktreeRequest(repo, "agent", "task-001"))
    assert exc.value.code == WorktreeErrorCode.PATH_CONFLICT
    assert sentinel.read_text(encoding="utf-8") == "user data"

    _git("branch", "newcode/subagent/task-002", cwd=repo)
    with pytest.raises(WorktreeError) as exc:
        manager.create(WorktreeRequest(repo, "agent", "task-002"))
    assert exc.value.code == WorktreeErrorCode.PATH_CONFLICT
    assert not (repo / ".newcode" / "worktrees" / "agent" / "task-002").exists()


def test_detached_head_and_invalid_repository_fail_before_resource_creation(tmp_path: Path):
    repo, _, _ = _repo(tmp_path)
    oid = _git("rev-parse", "HEAD", cwd=repo).stdout.decode().strip()
    _git("checkout", "--detach", oid, cwd=repo)
    manager = _manager(tmp_path)
    with pytest.raises(WorktreeError):
        manager.create(WorktreeRequest(repo, "agent", "task-detached"))
    assert not (repo / ".newcode" / "worktrees" / "agent" / "task-detached").exists()
    assert manager.leases() == ()


def test_owner_marker_tamper_and_missing_registration_are_rejected_without_removal(tmp_path: Path):
    repo, _, _ = _repo(tmp_path)
    manager = _manager(tmp_path)
    lease = manager.create(WorktreeRequest(repo, "agent", "task-owner"))
    marker = next((lease.common_git_dir / "newcode-worktrees" / "owners").glob("*.json"))
    original = marker.read_bytes()
    marker.write_text("{}", encoding="utf-8")
    with pytest.raises(WorktreeError) as exc:
        manager.verify(lease)
    assert exc.value.code == WorktreeErrorCode.REGISTRATION_INVALID
    assert lease.path.is_dir()
    marker.write_bytes(original)

    # 移除注册不是 Manager 的删除授权；不执行 worktree remove。
    _git("worktree", "remove", str(lease.path), cwd=repo)
    with pytest.raises(WorktreeError):
        manager.verify(lease)
    assert marker.exists()


def test_failed_post_add_verification_rolls_back_only_clean_owned_tree(tmp_path: Path, monkeypatch):
    repo, _, _ = _repo(tmp_path)
    manager = _manager(tmp_path)
    original = manager._verify_registration
    calls = 0

    def fail_once(lease):
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        return original(lease)

    monkeypatch.setattr(manager, "_verify_registration", fail_once)
    with pytest.raises(WorktreeError) as exc:
        manager.create(WorktreeRequest(repo, "agent", "task-rollback"))
    assert exc.value.code == WorktreeErrorCode.REGISTRATION_INVALID
    target = repo / ".newcode" / "worktrees" / "agent" / "task-rollback"
    assert not target.exists()
    assert not manager.leases()
    # 仅移除了刚创建的 worktree；禁止自动删 branch。
    assert manager._git.branch_exists(repo, "newcode/subagent/task-rollback")


def test_add_failure_without_created_resources_cleans_only_own_marker_and_next_task_works(tmp_path: Path, monkeypatch):
    repo, _, _ = _repo(tmp_path)
    manager = _manager(tmp_path)

    def fail_add(*_args):
        raise WorktreeError(WorktreeErrorCode.GIT_FAILED)

    monkeypatch.setattr(manager._git, "_manager_add_worktree", fail_add)
    with pytest.raises(WorktreeError):
        manager.create(WorktreeRequest(repo, "agent", "task-failed"))
    assert not (repo / ".newcode" / "worktrees" / "agent" / "task-failed").exists()
    assert not manager._git.branch_exists(repo, "newcode/subagent/task-failed")
    assert not manager.leases()

    monkeypatch.undo()
    lease = manager.create(WorktreeRequest(repo, "agent", "task-next"))
    assert manager.verify(lease) == lease


def test_cleanup_failure_preserves_registered_tree_and_marker(tmp_path: Path, monkeypatch):
    repo, _, _ = _repo(tmp_path)
    manager = _manager(tmp_path)
    original_verify = manager._verify_registration
    count = 0

    def fail_after_add_once(lease):
        nonlocal count
        count += 1
        return False if count == 1 else original_verify(lease)

    monkeypatch.setattr(manager, "_verify_registration", fail_after_add_once)

    def fail_remove(*_args):
        raise WorktreeError(WorktreeErrorCode.GIT_FAILED)

    monkeypatch.setattr(manager._git, "_manager_remove_worktree", fail_remove)
    with pytest.raises(WorktreeError):
        manager.create(WorktreeRequest(repo, "agent", "task-preserve"))
    lease = manager.leases()[0]
    assert lease.state is WorktreeState.CLEANUP_FAILED
    assert lease.path.is_dir()
    marker = next((lease.common_git_dir / "newcode-worktrees" / "owners").glob("*.json"))
    assert marker.exists()
    assert manager.diagnostics == ("worktree_cleanup_incomplete",)


def test_state_transition_table_rejects_terminal_or_skipped_transitions(tmp_path: Path):
    assert WorktreeState.CREATING in _TRANSITIONS[WorktreeState.VALIDATING]
    assert WorktreeState.READY not in _TRANSITIONS[WorktreeState.REQUESTED]
    assert _TRANSITIONS[WorktreeState.CLEANED] == frozenset()


def test_claim_occupancy_blocks_verify_and_release_records_terminal_state(tmp_path: Path):
    repo, _, _ = _repo(tmp_path)
    manager = _manager(tmp_path)
    ready = manager.create(WorktreeRequest(repo, "agent", "task-claim"))
    running = manager.claim(ready)
    assert running.state is WorktreeState.RUNNING
    with pytest.raises(WorktreeError) as exc:
        manager.verify(ready)
    assert exc.value.code == WorktreeErrorCode.LEASE_INVALID
    with pytest.raises(WorktreeError):
        manager.claim(ready)
    completed = manager.release(running, WorktreeState.COMPLETED)
    assert completed.state is WorktreeState.COMPLETED
    with pytest.raises(WorktreeError):
        manager.release(completed, WorktreeState.FAILED)


def test_manager_lock_file_excludes_second_owner_until_release(tmp_path: Path):
    lock_path = tmp_path / "metadata" / "locks" / "task.lock"
    lock_path.parent.mkdir(parents=True)
    with _FileLock(lock_path, 0.1):
        with pytest.raises(WorktreeError) as exc:
            with _FileLock(lock_path, 0.05):
                pytest.fail("同一 task 的第二个 owner 不应同时持锁")
        assert exc.value.code == WorktreeErrorCode.LOCK_TIMEOUT
    with _FileLock(lock_path, 0.1):
        pass


def test_stale_cleanup_removes_only_clean_verified_tree_and_keeps_branch(tmp_path: Path):
    repo, _, _ = _repo(tmp_path)
    creator = _manager(tmp_path)
    lease = creator.create(WorktreeRequest(repo, "agent", "stale-clean"))
    _set_local_upstream(repo, lease)
    _age_marker(lease, 31 * 24 * 60 * 60)
    cleaner = _manager(tmp_path)

    removed = cleaner.cleanup_stale(repo, now=time.time(), cleanup_after_days=30)

    assert removed == (lease.task_id,)
    assert not lease.path.exists()
    assert cleaner._git.branch_exists(repo, lease.branch)
    assert not _owner_marker(lease).exists()


def test_fresh_owner_marker_does_not_consume_stale_candidate_limit(tmp_path: Path):
    repo, _, _ = _repo(tmp_path)
    creator = _manager(tmp_path)
    fresh = creator.create(WorktreeRequest(repo, "agent", "fresh-first"))
    stale = creator.create(WorktreeRequest(repo, "agent", "stale-second"))
    _set_local_upstream(repo, stale)
    _age_marker(stale, 31 * 24 * 60 * 60)
    cleaner = _manager(tmp_path)

    removed = cleaner.cleanup_stale(repo, now=time.time(), max_candidates=1)

    assert removed == (stale.task_id,)
    assert fresh.path.is_dir()
    assert not stale.path.exists()


@pytest.mark.parametrize("dirty_kind", ["tracked", "staged", "untracked", "ignored_file", "ignored_directory", "ahead"])
def test_stale_cleanup_preserves_any_tracked_staged_untracked_ignored_or_ahead_state(tmp_path: Path, dirty_kind: str):
    repo, _, _ = _repo(tmp_path)
    # Commit ignore rules before the child checkout so both ignored forms are deterministic.
    with (repo / ".gitignore").open("a", encoding="utf-8") as file:
        file.write("ignored.txt\nignored-dir/\n")
    _git("add", ".gitignore", cwd=repo)
    _git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "ignore fixtures", cwd=repo)
    creator = _manager(tmp_path)
    lease = creator.create(WorktreeRequest(repo, "agent", f"stale-{dirty_kind}"))
    _set_local_upstream(repo, lease)
    if dirty_kind in {"tracked", "staged"}:
        (lease.path / "README.md").write_text("changed\n", encoding="utf-8")
        if dirty_kind == "staged":
            _git("add", "README.md", cwd=lease.path)
    elif dirty_kind == "untracked":
        (lease.path / "new.txt").write_text("untracked", encoding="utf-8")
    elif dirty_kind == "ignored_file":
        (lease.path / "ignored.txt").write_text("ignored", encoding="utf-8")
    elif dirty_kind == "ignored_directory":
        nested = lease.path / "ignored-dir"
        nested.mkdir()
        (nested / "child.txt").write_text("ignored", encoding="utf-8")
    else:
        (lease.path / "ahead.txt").write_text("ahead\n", encoding="utf-8")
        _git("add", "ahead.txt", cwd=lease.path)
        _git("-c", "core.hooksPath=" + str(tmp_path / "manager-empty-hooks"), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "ahead", cwd=lease.path)
    _age_marker(lease, 31 * 24 * 60 * 60)
    cleaner = _manager(tmp_path)

    removed = cleaner.cleanup_stale(repo, now=time.time(), cleanup_after_days=30)

    assert removed == ()
    assert lease.path.is_dir()
    assert _owner_marker(lease).exists()
    assert "worktree_preserved_changes" in cleaner.diagnostics


def test_stale_cleanup_preserves_when_upstream_missing_or_status_incomplete(tmp_path: Path, monkeypatch):
    repo, _, _ = _repo(tmp_path)
    creator = _manager(tmp_path)
    lease = creator.create(WorktreeRequest(repo, "agent", "stale-unknown"))
    _age_marker(lease, 31 * 24 * 60 * 60)
    cleaner = _manager(tmp_path)

    assert cleaner.cleanup_stale(repo, now=time.time()) == ()
    assert lease.path.is_dir()
    _set_local_upstream(repo, lease)
    monkeypatch.setattr(cleaner._git, "ignored_paths", lambda _path: (_ for _ in ()).throw(WorktreeError("query_failed")))
    assert cleaner.cleanup_stale(repo, now=time.time()) == ()
    assert lease.path.is_dir()


def test_stale_cleanup_preserves_remote_tracking_upstream_without_network(tmp_path: Path, monkeypatch):
    repo, _, _ = _repo(tmp_path)
    creator = _manager(tmp_path)
    lease = creator.create(WorktreeRequest(repo, "agent", "stale-remote"))
    _git("update-ref", "refs/remotes/origin/main", lease.base_commit, cwd=repo)
    _git("config", f"branch.{lease.branch}.remote", "origin", cwd=repo)
    _git("config", f"branch.{lease.branch}.merge", "refs/heads/main", cwd=repo)
    _age_marker(lease, 31 * 24 * 60 * 60)
    cleaner = _manager(tmp_path)
    commands = []
    original_execute = cleaner._git._execute

    def record_execute(cwd, args, **kwargs):
        commands.append(tuple(args))
        return original_execute(cwd, args, **kwargs)

    monkeypatch.setattr(cleaner._git, "_execute", record_execute)
    assert cleaner.cleanup_stale(repo, now=time.time()) == ()
    assert lease.path.is_dir()
    assert not any(args and args[0] in {"fetch", "pull", "push"} for args in commands)
    assert any(args[:2] == ("rev-parse", "--symbolic-full-name") for args in commands)


def test_stale_cleanup_preserves_at_exact_age_and_when_task_lock_is_active(tmp_path: Path):
    repo, _, _ = _repo(tmp_path)
    creator = _manager(tmp_path)
    lease = creator.create(WorktreeRequest(repo, "agent", "stale-lock"))
    _set_local_upstream(repo, lease)
    fixed_now = time.time()
    os.utime(_owner_marker(lease), (fixed_now - 30 * 24 * 60 * 60,) * 2)
    cleaner = _manager(tmp_path)
    assert cleaner.cleanup_stale(repo, now=fixed_now, cleanup_after_days=30) == ()
    assert lease.path.is_dir()

    _age_marker(lease, 31 * 24 * 60 * 60)
    with _FileLock(_task_lock_path_for_test(lease), 0.1):
        assert cleaner.cleanup_stale(repo, now=time.time(), cleanup_after_days=30) == ()
    assert lease.path.is_dir()


def test_claimed_task_holds_cross_process_activity_lock_until_release(tmp_path: Path):
    repo, _, _ = _repo(tmp_path)
    creator = _manager(tmp_path)
    ready = creator.create(WorktreeRequest(repo, "agent", "stale-active"))
    _set_local_upstream(repo, ready)
    running = creator.claim(ready)
    _age_marker(running, 31 * 24 * 60 * 60)
    cleaner = _manager(tmp_path)

    assert cleaner.cleanup_stale(repo, now=time.time()) == ()
    assert running.path.is_dir()
    creator.release(running, WorktreeState.COMPLETED)
    assert cleaner.cleanup_stale(repo, now=time.time()) == (running.task_id,)
    assert not running.path.exists()


def test_stale_cleanup_rejects_tampered_owner_and_symlink_path_without_touching_target(tmp_path: Path):
    repo, _, _ = _repo(tmp_path)
    creator = _manager(tmp_path)
    lease = creator.create(WorktreeRequest(repo, "agent", "stale-link"))
    _set_local_upstream(repo, lease)
    _age_marker(lease, 31 * 24 * 60 * 60)
    marker = _owner_marker(lease)
    original_marker = marker.read_bytes()
    marker.write_text("{}", encoding="utf-8")
    cleaner = _manager(tmp_path)
    assert cleaner.cleanup_stale(repo, now=time.time()) == ()
    assert lease.path.is_dir()

    marker.write_bytes(original_marker)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    moved = tmp_path / "preserved-worktree"
    lease.path.rename(moved)
    try:
        lease.path.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        moved.rename(lease.path)
        pytest.skip(f"当前环境不能创建目录符号链接：{type(exc).__name__}")
    assert cleaner.cleanup_stale(repo, now=time.time()) == ()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert lease.path.is_symlink()


def test_stale_cleanup_failure_isolated_per_candidate(tmp_path: Path, monkeypatch):
    repo, _, _ = _repo(tmp_path)
    creator = _manager(tmp_path)
    first = creator.create(WorktreeRequest(repo, "agent", "stale-one"))
    second = creator.create(WorktreeRequest(repo, "agent", "stale-two"))
    _set_local_upstream(repo, first)
    _set_local_upstream(repo, second)
    _age_marker(first, 31 * 24 * 60 * 60)
    _age_marker(second, 31 * 24 * 60 * 60)
    cleaner = _manager(tmp_path)
    original_remove = cleaner._git._manager_remove_worktree
    calls = []

    def fail_once(cwd, path):
        calls.append(Path(path))
        if len(calls) == 1:
            raise WorktreeError(WorktreeErrorCode.CLEANUP_FAILED)
        return original_remove(cwd, path)

    monkeypatch.setattr(cleaner._git, "_manager_remove_worktree", fail_once)
    removed = cleaner.cleanup_stale(repo, now=time.time())

    assert len(calls) == 2
    assert len(removed) == 1
    assert sum(path.exists() for path in (first.path, second.path)) == 1


def _task_lock_path_for_test(lease: WorktreeLease) -> Path:
    import hashlib
    key = hashlib.sha256(lease.task_id.encode("utf-8")).hexdigest()
    return lease.common_git_dir / "newcode-worktrees" / "locks" / f"task-{key}.lock"
