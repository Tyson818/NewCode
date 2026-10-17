"""Team 专用 lease 恢复；只访问临时本地 Git 仓库。"""

from dataclasses import replace
import json
import subprocess
import sys

import pytest

from newcode.teams.types import TeamError
from test_teams_members import setup_member, reopened, member_record
from test_subagents_worktrees import _git


def test_team_resume_keeps_branch_base_and_own_changes(tmp_path):
    runner, kwargs, _ = setup_member(tmp_path)
    runner.activate()
    lease = runner._lease
    (lease.path / "README.md").write_text("member result", encoding="utf-8")
    runner.close()
    candidate = reopened(runner, kwargs)
    try:
        candidate.open()
        assert candidate._lease.path == lease.path
        assert candidate._lease.branch == lease.branch
        assert candidate._lease.base_commit == lease.base_commit
        assert (lease.path / "README.md").read_text() == "member result"
        assert (lease.repository_root / "README.md").read_text() == "base contents\n"
    finally:
        candidate.close()


@pytest.mark.parametrize("tamper", ["owner", "head", "branch", "binding", "registration", "common", "path", "actual-head", "actual-branch"])
def test_team_resume_rejects_unknown_ownership_without_provider_or_delete(tmp_path, tamper):
    runner, kwargs, _ = setup_member(tmp_path)
    runner.activate()
    lease = runner._lease
    runner.close()
    from newcode.worktrees.manager import _marker_file, _metadata_root
    marker = _marker_file(_metadata_root(lease.common_git_dir), lease.task_id)
    if tamper == "owner":
        value = json.loads(marker.read_text()); value["owner_token"] = "0"*64
        marker.write_text(json.dumps(value), encoding="utf-8")
    elif tamper in {"head", "branch", "common", "path"}:
        value = json.loads(marker.read_text())
        field = {"head": "base_commit", "branch": "branch", "common": "common_git_dir", "path": "worktree_path"}[tamper]
        value[field] = "0"*40 if tamper == "head" else str(tmp_path / "wrong")
        marker.write_text(json.dumps(value), encoding="utf-8")
    elif tamper == "binding":
        path = runner.store.paths.root / runner.scope.team_id / "members" / "alice" / "worktree.json"
        value = json.loads(path.read_text()); value["team_id"] = "team-"+"0"*32
        path.write_text(json.dumps(value), encoding="utf-8")
    elif tamper == "actual-head":
        _git(lease.path, "-c", f"core.hooksPath={tmp_path / 'manager-empty-hooks'}", "-c", "user.name=Fixture",
             "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "external fixture change")
    elif tamper == "actual-branch":
        _git(lease.path, "-c", f"core.hooksPath={tmp_path / 'manager-empty-hooks'}", "checkout", "--detach")
    else:
        # 仅 fixture 注入只读 Git 查询异常，生产不放宽 ownership 检查。
        pass
    candidate = reopened(runner, kwargs)
    if tamper == "registration":
        candidate.worktrees._git.list_worktrees = lambda _: ()
    before = marker.read_bytes()
    with pytest.raises((TeamError, ValueError)):
        candidate.open()
    assert not candidate.factory.created
    assert lease.path.exists() and marker.read_bytes() == before


def test_actual_process_owner_lock_blocks_cross_generation_takeover(tmp_path):
    runner, kwargs, _ = setup_member(tmp_path)
    runner.activate()
    runner.close()
    code = """
import sys
from newcode.teams.paths import TeamPaths
from newcode.teams.store import TeamStore
from newcode.teams.runtime import StoreExecutorLeases
from newcode.teams.types import TeamScope
from newcode.worktrees.manager import WorktreeManager
from newcode.worktrees.types import WorktreeState
from pathlib import Path
s=TeamStore(TeamPaths(Path(sys.argv[1]),common_git_dir=Path(sys.argv[2]),user_home=Path(sys.argv[3])))
scope=TeamScope(sys.argv[4],'alice',sys.argv[5],2)
lease=StoreExecutorLeases(s).acquire(scope)
manager=WorktreeManager(empty_hooks_dir=Path(sys.argv[6]))
worktree=manager.claim_team_member(s,scope,'worker')
print('owned',flush=True)
sys.stdin.readline()
manager.release(worktree,WorktreeState.COMPLETED)
lease.__exit__(None,None,None)
"""
    paths = runner.store.paths
    process = subprocess.Popen([sys.executable, "-c", code, str(paths.workspace_root), str(paths.common_git_dir),
                                str(tmp_path / "home"), runner.scope.team_id, runner.scope.session_id,
                                str(tmp_path / "manager-empty-hooks")],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "owned"
        candidate = reopened(runner, kwargs)
        with pytest.raises(TeamError, match="team_member_busy"):
            candidate.open()
        assert not candidate.factory.created
    finally:
        process.communicate("stop\n", timeout=10)
        assert process.returncode == 0
    candidate.open()
    candidate.close()


def test_git_activity_lock_is_independent_restore_gate(tmp_path):
    from newcode.worktrees.manager import _FileLock, _task_lock_path
    runner, kwargs, _ = setup_member(tmp_path)
    runner.activate()
    lease = runner._lease
    runner.close()
    candidate = reopened(runner, kwargs)
    with _FileLock(_task_lock_path(lease), 0):
        with pytest.raises(ValueError):
            candidate.open()
        assert not candidate.factory.created
        assert lease.path.exists()
    candidate.close()


def test_same_runtime_resume_revalidates_owner_before_new_request(tmp_path):
    from newcode.worktrees.manager import _marker_file, _metadata_root
    runner, _, provider = setup_member(tmp_path)
    try:
        runner.activate()
        marker = _marker_file(_metadata_root(runner._lease.common_git_dir), runner._lease.task_id)
        raw = json.loads(marker.read_text()); raw["owner_token"] = "0" * 64
        marker.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ValueError):
            runner.resume()
        assert len(provider.requests) == 1
    finally:
        runner.close()
