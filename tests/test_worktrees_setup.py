from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from newcode.worktrees.git import WorktreeGitAdapter
from newcode.worktrees.setup import initialize_worktree
from newcode.worktrees.types import WorktreeError, WorktreeErrorCode, WorktreeSetupPolicy


def test_initialization_copies_only_exact_allowlisted_plain_files(tmp_path: Path):
    source = tmp_path / "repository"
    child = tmp_path / "worktree"
    source.mkdir()
    child.mkdir()
    (source / "README.md").write_text("local readme", encoding="utf-8")
    (source / "AGENTS.md").write_text("local instructions", encoding="utf-8")
    (source / ".env").write_text("secret", encoding="utf-8")
    policy = WorktreeSetupPolicy(
        copy_files=frozenset({"README.md"}),
        copy_ignored_files=frozenset({"AGENTS.md"}),
    )

    result = initialize_worktree(
        source, child, policy,
        is_tracked=lambda name: name == "README.md",
        is_ignored=lambda name: name == "AGENTS.md",
    )

    assert result.copied_files == ("AGENTS.md", "README.md")
    assert (child / "README.md").read_text(encoding="utf-8") == "local readme"
    assert (child / "AGENTS.md").read_text(encoding="utf-8") == "local instructions"
    assert not (child / ".env").exists()
    assert result.hooks_enabled is False


@pytest.mark.parametrize("name", [".env", "README.md/child", "../README.md", ".git/config"])
def test_invalid_or_non_file_allowlist_fails_without_launchable_setup(tmp_path: Path, name: str):
    source = tmp_path / "repository"
    child = tmp_path / "worktree"
    source.mkdir()
    child.mkdir()
    (source / "README.md").write_text("source", encoding="utf-8")

    with pytest.raises(WorktreeError) as exc:
        initialize_worktree(
            source, child, WorktreeSetupPolicy(copy_files=frozenset({name})),
            is_tracked=lambda _name: True, is_ignored=lambda _name: False,
        )

    assert exc.value.code == WorktreeErrorCode.SETUP_FAILED
    assert not list(child.rglob("*.tmp"))


def test_copy_rejects_directory_in_place_of_allowlisted_file(tmp_path: Path):
    source = tmp_path / "repository"
    child = tmp_path / "worktree"
    source.mkdir()
    child.mkdir()
    (source / "README.md").mkdir()
    (source / "README.md" / "nested.txt").write_text("not recursive", encoding="utf-8")

    with pytest.raises(WorktreeError) as exc:
        initialize_worktree(
            source, child, WorktreeSetupPolicy(copy_files=frozenset({"README.md"})),
            is_tracked=lambda _name: True, is_ignored=lambda _name: False,
        )

    assert exc.value.code == WorktreeErrorCode.SETUP_FAILED
    assert not (child / "README.md" / "nested.txt").exists()


def test_atomic_copy_failure_leaves_no_temp_or_partial_destination(tmp_path: Path, monkeypatch):
    import newcode.worktrees.setup as setup

    source = tmp_path / "repository"
    child = tmp_path / "worktree"
    source.mkdir()
    child.mkdir()
    (source / "README.md").write_text("new value", encoding="utf-8")
    (child / "README.md").write_text("old value", encoding="utf-8")

    def fail_replace(*_args):
        raise OSError("fixture failure")

    monkeypatch.setattr(setup.os, "replace", fail_replace)
    with pytest.raises(WorktreeError):
        initialize_worktree(
            source, child, WorktreeSetupPolicy(copy_files=frozenset({"README.md"})),
            is_tracked=lambda _name: True, is_ignored=lambda _name: False,
        )

    assert (child / "README.md").read_text(encoding="utf-8") == "old value"
    assert sorted(path.name for path in child.iterdir()) == ["README.md"]


@pytest.mark.parametrize(
    ("policy", "tracked", "ignored"),
    [
        (WorktreeSetupPolicy(copy_files=frozenset({"README.md"})), False, False),
        (WorktreeSetupPolicy(copy_ignored_files=frozenset({"README.md"})), False, False),
    ],
)
def test_source_classification_mismatch_fails_before_any_copy(tmp_path: Path, policy, tracked, ignored):
    source = tmp_path / "repository"
    child = tmp_path / "worktree"
    source.mkdir()
    child.mkdir()
    (source / "README.md").write_text("source", encoding="utf-8")

    with pytest.raises(WorktreeError) as exc:
        initialize_worktree(
            source, child, policy,
            is_tracked=lambda _name: tracked,
            is_ignored=lambda _name: ignored,
        )

    assert exc.value.code == WorktreeErrorCode.SETUP_FAILED
    assert list(child.iterdir()) == []


def test_git_source_classification_uses_fixed_read_only_queries(tmp_path: Path):
    import subprocess

    repo = tmp_path / "repository"
    repo.mkdir()
    hooks = tmp_path / "manager-empty-hooks"
    hooks.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, shell=False, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "config", "core.hooksPath", str(hooks)], check=True, shell=False, capture_output=True)
    (repo / "README.md").write_text("tracked", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md", ".gitignore"], check=True, shell=False, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture"], check=True, shell=False, capture_output=True)
    (repo / "ignored.txt").write_text("ignored", encoding="utf-8")
    adapter = WorktreeGitAdapter(empty_hooks_dir=hooks)

    assert adapter.is_tracked_path(repo, "README.md") is True
    assert adapter.is_tracked_path(repo, "ignored.txt") is False
    assert adapter.is_ignored_path(repo, "ignored.txt") is True
    assert adapter.is_ignored_path(repo, "README.md") is False


def test_dependency_link_is_skipped_without_proven_child_read_only_enforcement(tmp_path: Path):
    source = tmp_path / "repository"
    child = tmp_path / "worktree"
    dependency = tmp_path / "trusted-dependency"
    source.mkdir()
    child.mkdir()
    dependency.mkdir()

    result = initialize_worktree(
        source,
        child,
        WorktreeSetupPolicy(dependency_roots={"trusted": dependency.resolve()}),
    )

    assert result.diagnostics == ("worktree_dependency_link_skipped",)
    assert not (child / "trusted").exists()


def test_hooks_path_must_remain_empty_and_outside_repository_before_git_launch(tmp_path: Path):
    repo = tmp_path / "repository"
    repo.mkdir()
    hooks = tmp_path / "manager-empty-hooks"
    hooks.mkdir()
    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"a" * 40 + b"\n", stderr=b"")

    adapter = WorktreeGitAdapter(empty_hooks_dir=hooks, executable="git-fake", runner=fake_runner)
    (hooks / "post-checkout").write_text("fixture", encoding="utf-8")
    with pytest.raises(WorktreeError):
        adapter.head(repo)
    assert calls == []

    empty_inside_repo = repo / "empty-hooks"
    empty_inside_repo.mkdir()
    inside_adapter = WorktreeGitAdapter(
        empty_hooks_dir=empty_inside_repo, executable="git-fake", runner=fake_runner,
    )
    with pytest.raises(WorktreeError):
        inside_adapter.head(repo)
    assert calls == []
