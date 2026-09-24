from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from newcode.worktrees.paths import (
    build_worktree_path,
    validate_branch,
    validate_relative_name,
)
from newcode.worktrees.types import WorktreeError, WorktreeErrorCode


@pytest.mark.parametrize("value", ["", "/abs", "C:/x", "//host/share", "a\\b", "a//b", "a/./b", "a/../b", "a:", "a\n"])
def test_relative_name_rejects_unsafe_forms(value: str):
    with pytest.raises(WorktreeError) as exc:
        validate_relative_name(value)
    assert exc.value.code == WorktreeErrorCode.PATH_INVALID


def test_relative_name_segment_and_length_boundaries():
    assert validate_relative_name("one/two/three") == ("one", "two", "three")
    with pytest.raises(WorktreeError):
        validate_relative_name("a/b/c/d")
    with pytest.raises(WorktreeError):
        validate_relative_name("a" * 161)


def test_build_worktree_path_generates_contained_path_and_branch(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = build_worktree_path(repo, "review_agent-1", "Abc_123")
    assert result.repository_root == repo.resolve()
    assert result.worktree_root == repo.resolve() / ".newcode" / "worktrees"
    assert result.relative_name == "review_agent-1/Abc_123"
    assert result.checkout_path == result.worktree_root / "review_agent-1" / "Abc_123"
    assert result.branch == "newcode/subagent/Abc_123"
    with pytest.raises(WorktreeError) as exc:
        build_worktree_path(repo, "../escape", "task")
    assert exc.value.code == WorktreeErrorCode.PATH_INVALID


def test_build_rejects_existing_path_and_symlink_parent(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    existing = repo / ".newcode" / "worktrees" / "agent" / "task"
    existing.mkdir(parents=True)
    with pytest.raises(WorktreeError) as exc:
        build_worktree_path(repo, "agent", "task")
    assert exc.value.code == WorktreeErrorCode.PATH_CONFLICT

    other = tmp_path / "outside"
    other.mkdir()
    worktree_root = repo / ".newcode" / "worktrees"
    worktree_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        worktree_root.symlink_to(other, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建目录符号链接")
    with pytest.raises(WorktreeError):
        build_worktree_path(repo, "agent", "new-task")


def test_build_rejects_reparse_point_resolving_outside_without_side_effects(tmp_path: Path, monkeypatch):
    """模拟 Windows junction，确保环境不支持真实 symlink 时仍验证越界拒绝。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".newcode").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    worktree_root = repo / ".newcode" / "worktrees"
    original_lstat = Path.lstat
    original_resolve = Path.resolve

    def fake_lstat(path: Path):
        if path == worktree_root:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o755,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            )
        return original_lstat(path)

    def fake_resolve(path: Path, *args, **kwargs):
        if path == worktree_root:
            return outside.resolve()
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(Path, "resolve", fake_resolve)
    assert worktree_root.resolve(strict=False) == outside.resolve()

    with pytest.raises(WorktreeError) as exc:
        build_worktree_path(repo, "agent", "task")

    assert exc.value.code == WorktreeErrorCode.PATH_INVALID
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert list(outside.iterdir()) == [sentinel]
    assert not (outside / "agent" / "task").exists()
    assert not (repo / ".newcode" / "worktrees").exists()


@pytest.mark.parametrize("branch", ["main", "newcode/subagent/../x", "newcode/subagent/a.lock", "newcode/subagent/a b"])
def test_branch_syntax_rejects_non_manager_branch(branch: str):
    with pytest.raises(WorktreeError):
        validate_branch(branch, lambda _value: True)


def test_branch_requires_git_ref_checker_success():
    assert validate_branch("newcode/subagent/task_1", lambda _value: True) == "newcode/subagent/task_1"
    with pytest.raises(WorktreeError):
        validate_branch("newcode/subagent/task_1", lambda _value: False)
