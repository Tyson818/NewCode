from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from newcode.worktrees.git import ChildGitReadOnlyGate, WorktreeGitAdapter
from newcode.worktrees.types import WorktreeError, WorktreeErrorCode


class FakeRunner:
    def __init__(self, result=None, error=None):
        self.result = result or SimpleNamespace(returncode=0, stdout=b"abc\n", stderr=b"")
        self.error = error
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.error:
            raise self.error
        return self.result


def _adapter(tmp_path: Path, runner: FakeRunner, **kwargs):
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return WorktreeGitAdapter(
        empty_hooks_dir=hooks,
        executable="git-fake",
        runner=runner,
        base_env={"PATH": "test-path", "HOME": "private-home", "GIT_DIR": "injected", "GIT_CONFIG_COUNT": "8"},
        **kwargs,
    ), repo


def test_manager_git_queries_use_fixed_argv_shell_false_hooks_and_controlled_environment(tmp_path: Path):
    runner = FakeRunner(SimpleNamespace(returncode=0, stdout=b"true\n", stderr=b""))
    adapter, repo = _adapter(tmp_path, runner)
    assert adapter.is_bare_repository(repo)
    argv, options = runner.calls[0]
    assert argv[1:3] == ["-c", f"core.hooksPath={tmp_path / 'empty-hooks'}"]
    assert ["-c", "core.fsmonitor=false"] == argv[3:5]
    assert "--no-pager" in argv
    assert argv[-2:] == ["rev-parse", "--is-bare-repository"]
    assert options["shell"] is False
    assert options["cwd"] == str(repo.resolve())
    env = options["env"]
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == __import__("os").devnull
    assert env["GIT_CONFIG_COUNT"] == "0"
    assert "GIT_DIR" not in env and "HOME" not in env


@pytest.mark.parametrize("method,args", [("status", ()), ("head", ())])
def test_every_manager_command_includes_hooks_override(tmp_path: Path, method: str, args: tuple):
    runner = FakeRunner(SimpleNamespace(returncode=0, stdout=(b"" if method == "status" else b"a" * 40 + b"\n"), stderr=b""))
    adapter, repo = _adapter(tmp_path, runner)
    getattr(adapter, method)(repo, *args)
    assert all("core.hooksPath=" in call[0][2] for call in runner.calls)


def test_all_fixed_manager_queries_override_hooks_and_never_accept_arbitrary_subcommands(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git_dir = repo / ".git"
    git_dir.mkdir()
    oid = "a" * 40

    class DispatchRunner:
        def __init__(self):
            self.calls = []

        def __call__(self, argv, **kwargs):
            self.calls.append((argv, kwargs))
            args = argv[argv.index("-C") + 2:]
            if args[-1:] == ["--show-toplevel"]:
                out = str(repo).encode() + b"\n"
            elif args[-1:] == ["--git-common-dir"]:
                out = str(git_dir).encode() + b"\n"
            elif args[-1:] == ["--is-bare-repository"]:
                out = b"false\n"
            elif args[-2:] == ["--verify", "HEAD"]:
                out = (oid + "\n").encode()
            elif args[:2] == ["rev-parse", "--symbolic-full-name"]:
                out = b"refs/heads/main\n"
            elif args[0] == "symbolic-ref":
                out = b"main\n"
            elif args[:3] == ["worktree", "list", "--porcelain"]:
                out = f"worktree {repo}\nHEAD {oid}\nbranch refs/heads/main\n\n".encode()
            elif args[0] == "rev-list":
                out = b"0\n"
            elif args[0] == "check-ref-format":
                out = b"newcode/subagent/task\n"
            else:
                out = b""
            return SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    runner = DispatchRunner()
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    adapter = WorktreeGitAdapter(empty_hooks_dir=hooks, executable="git-fake", runner=runner)
    assert adapter.repository_root(repo) == repo
    assert adapter.common_git_dir(repo) == git_dir
    assert not adapter.is_bare_repository(repo)
    assert adapter.head(repo) == oid
    assert adapter.current_branch(repo) == "main"
    assert adapter.list_worktrees(repo)[0].startswith("worktree ")
    assert adapter.status(repo) == b""
    assert adapter.status(repo, include_ignored=True) == b""
    assert adapter.ignored_paths(repo) == b""
    assert adapter.upstream_ahead_count(repo) == 0
    assert adapter.check_branch_name(repo, "newcode/subagent/task")
    assert len(runner.calls) == 12
    assert any(
        argv[argv.index("-C") + 2:][:2] == ["rev-parse", "--symbolic-full-name"]
        for argv, _ in runner.calls
    )
    for argv, options in runner.calls:
        assert argv[1:3] == ["-c", f"core.hooksPath={hooks.resolve()}"]
        assert ["-c", "core.fsmonitor=false"] == argv[3:5]
        assert "--no-pager" in argv
        assert options["shell"] is False

    # Adapter 没有接受任意 argv/subcommand 的 public 方法。
    assert not hasattr(adapter, "execute")
    assert not hasattr(adapter, "run")


def test_manager_adapter_has_no_mutation_api_and_child_writes_reject_before_capability():
    calls = []

    class Capability:
        def run_readonly_git(self, *, argv, cwd):
            calls.append((argv, cwd))
            return b"ok"

    gate = ChildGitReadOnlyGate(cwd=Path("."), capability=Capability())
    assert gate.execute(("git", "status", "--no-optional-locks")) == b"ok"
    assert len(calls) == 1
    for command in (
        ("git", "add", "file.py"),
        ("git", "commit", "-m", "x"),
        ("git", "update-ref", "refs/heads/x", "HEAD"),
        ("git", "-c", "core.hooksPath=x", "status"),
        ("git", "status", "--", "../../outside"),
    ):
        with pytest.raises(WorktreeError) as exc:
            gate.execute(command)
        assert exc.value.code == WorktreeErrorCode.GIT_FAILED
    assert len(calls) == 1


def test_child_git_fails_closed_without_verified_sandbox_capability():
    gate = ChildGitReadOnlyGate(cwd=Path("."))
    with pytest.raises(WorktreeError):
        gate.execute(("git", "status"))


@pytest.mark.parametrize(
    "error,code",
    [(subprocess.TimeoutExpired("git", 0.1), WorktreeErrorCode.GIT_TIMEOUT), (OSError("secret path"), WorktreeErrorCode.GIT_UNAVAILABLE)],
)
def test_git_failures_are_stable_and_redacted(tmp_path: Path, error: Exception, code: str):
    runner = FakeRunner(error=error)
    adapter, repo = _adapter(tmp_path, runner, timeout_seconds=0.1)
    with pytest.raises(WorktreeError) as exc:
        adapter.head(repo)
    assert exc.value.code == code
    assert "secret" not in str(exc.value)


def test_git_output_limit_and_invalid_output_are_rejected(tmp_path: Path):
    runner = FakeRunner(SimpleNamespace(returncode=0, stdout=b"x" * 33, stderr=b""))
    adapter, repo = _adapter(tmp_path, runner, max_output_bytes=32)
    with pytest.raises(WorktreeError) as exc:
        adapter.head(repo)
    assert exc.value.code == WorktreeErrorCode.GIT_OUTPUT_INVALID

    runner = FakeRunner(SimpleNamespace(returncode=0, stdout=b"not-an-object-id\n", stderr=b""))
    adapter, repo = _adapter(tmp_path, runner)
    with pytest.raises(WorktreeError):
        adapter.head(repo)
