from __future__ import annotations

from pathlib import Path

import pytest

from newcode.worktrees.setup import load_worktree_config


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_project_config_is_subset_of_user_ceiling_and_cleanup_floor(tmp_path: Path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    dep = tmp_path / "trusted-dep"
    dep.mkdir()
    _write(
        home / ".newcode" / "worktree.yaml",
        "copy_files: [README.md, pyproject.toml]\ncopy_ignored_files: [AGENTS.md]\ndependency_roots:\n  shared: " + str(dep).replace("\\", "/") + "\ncleanup_after_days: 45\n",
    )
    _write(
        repo / ".newcode" / "worktree.yaml",
        "copy_files: [README.md, uv.lock]\ncopy_ignored_files: [AGENTS.md]\ndependency_roots: [shared, unknown]\ncleanup_after_days: 30\n",
    )
    result = load_worktree_config(repo, user_home=home)
    assert result.policy.copy_files == {"README.md"}
    assert result.policy.copy_ignored_files == {"AGENTS.md"}
    assert dict(result.policy.dependency_roots) == {"shared": dep.resolve()}
    assert result.policy.cleanup_after_days == 45
    assert any(item.code == "worktree_config_entry_rejected" for item in result.diagnostics)


def test_default_and_invalid_project_config_never_expands_user_policy(tmp_path: Path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    _write(home / ".newcode" / "worktree.yaml", "copy_files: [README.md]\n")
    _write(repo / ".newcode" / "worktree.yaml", "copy_files: [README.md]\nunknown: true\n")
    result = load_worktree_config(repo, user_home=home)
    assert result.policy.copy_files == frozenset()
    assert result.policy.cleanup_after_days == 30
    assert result.diagnostics == (type(result.diagnostics[0])("project", "worktree_config_invalid"),)


@pytest.mark.parametrize("name", [".env", ".env.local", "credentials.yaml", "api_key.txt", "private_key.pem", ".git/config", ".newcode/context-artifacts/a"])
def test_sensitive_paths_are_rejected_even_when_explicitly_requested(tmp_path: Path, name: str):
    home, repo = tmp_path / "home", tmp_path / "repo"
    _write(home / ".newcode" / "worktree.yaml", f"copy_files: [{name!r}]\n")
    result = load_worktree_config(repo, user_home=home)
    assert result.policy.copy_files == frozenset()
    assert all(name not in repr(item) for item in result.diagnostics)


def test_duplicate_yaml_keys_and_bad_user_config_fail_closed(tmp_path: Path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    _write(home / ".newcode" / "worktree.yaml", "copy_files: [README.md]\ncopy_files: [pyproject.toml]\n")
    _write(repo / ".newcode" / "worktree.yaml", "copy_files: [README.md]\n")
    result = load_worktree_config(repo, user_home=home)
    assert result.policy.copy_files == frozenset()
    assert any(item.source == "user" and item.code == "worktree_config_invalid" for item in result.diagnostics)


def test_gitignore_rule_is_precise_in_temporary_repository(tmp_path: Path):
    # Verify the exact rule currently intended for this chapter using a temporary Git repo.
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    project_gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
    (repo / ".gitignore").write_text(project_gitignore.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "init", str(repo)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", ".newcode/worktrees/agent/task"], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    instructions = subprocess.run(["git", "-C", str(repo), "check-ignore", ".newcode/INSTRUCTIONS.md"], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    assert ignored.returncode == 0
    assert instructions.returncode == 1
