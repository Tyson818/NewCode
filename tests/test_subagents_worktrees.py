from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from newcode.agent.mode import AgentMode
from newcode.permissions.manager import PermissionManager
from newcode.permissions.types import PermissionMode
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.session import ChatMessage
from newcode.subagents.manager import SubAgentManager
from newcode.subagents.runner import DefinitionTask, SubAgentRunner
from newcode.subagents.types import (
    AgentDefinition,
    AgentIsolation,
    AgentPermissionMode,
    AgentSource,
    ParentPolicySnapshot,
    TaskState,
    WorkerResult,
)
from newcode.tools.registry import ToolRegistry, create_default_registry
from newcode.tools.types import ToolCall, ToolContext
from newcode.worktrees.git import WorktreeGitAdapter
from newcode.worktrees.manager import WorktreeManager
from newcode.worktrees.types import WorktreeSetupPolicy, WorktreeState


def _git(repo: Path, *args: str):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, shell=False, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _repository(tmp_path: Path) -> tuple[Path, WorktreeManager]:
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"},
        shell=False, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    (repo / ".gitignore").write_text(".newcode/worktrees/\n", encoding="utf-8")
    (repo / "README.md").write_text("base contents\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "README.md")
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "initial")
    hooks = tmp_path / "manager-empty-hooks"
    hooks.mkdir()
    adapter = WorktreeGitAdapter(
        empty_hooks_dir=hooks,
        executable=shutil.which("git") or "git",
    )
    return repo, WorktreeManager(empty_hooks_dir=hooks, git=adapter)


class _Factory:
    available_models = frozenset({"test-model"})

    def __init__(self, provider):
        self.provider = provider
        self.created = []

    def create(self, model, *, timeout_seconds):
        self.created.append((model, timeout_seconds))
        return self.provider


class _WriteThenFinishProvider:
    def __init__(self):
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append({"messages": tuple(messages), "tools": tuple(tools or ())})
        if len(self.calls) == 1:
            yield ToolCallEvent([ToolCall("write-1", "write_file", {"path": "README.md", "content": "child contents\n"})])
        else:
            yield TextDelta("worktree task finished")


class _UnusedProvider:
    def stream_chat(self, *_args, **_kwargs):
        raise AssertionError("Provider must not start")


def _definition(*, allow=("read_file", "write_file", "run_command")) -> AgentDefinition:
    return AgentDefinition(
        name="worker", description="worktree worker", source=AgentSource.BUILTIN,
        tools_allow=tuple(allow), tools_deny=(), max_iterations=4,
        permission_mode=AgentPermissionMode.TRUSTED, body="",
        isolation=AgentIsolation.WORKTREE,
    )


def _runtime(tmp_path: Path, provider, *, registry: ToolRegistry | None = None, policy=None):
    repo, worktrees = _repository(tmp_path)
    registry = registry or create_default_registry()
    holder = {}
    manager = SubAgentManager(lambda task: holder["runner"](task))
    scope = manager.open_session("parent-session")
    manager.publish_policy_snapshot(
        scope, ParentPolicySnapshot(scope, frozenset(registry.names()), PermissionMode.TRUSTED),
    )
    factory = _Factory(provider)
    runner = SubAgentRunner(
        manager=manager,
        provider_factory=factory,
        default_model="test-model",
        registry=registry,
        tool_context=ToolContext(repo),
        parent_permission_manager=PermissionManager(mode=PermissionMode.TRUSTED),
        worktree_manager=worktrees,
        worktree_setup_policy=policy or WorktreeSetupPolicy(),
    )
    holder["runner"] = runner
    return repo, worktrees, manager, scope, runner, factory, registry


def test_worktree_definition_writes_only_child_and_returns_safe_path_status(tmp_path: Path):
    provider = _WriteThenFinishProvider()
    repo, worktrees, manager, scope, _runner, factory, _registry = _runtime(tmp_path, provider)
    parent = ChatMessage("user", "parent history stays elsewhere")
    try:
        task = manager.start(scope, "edit isolated copy", payload=DefinitionTask(_definition(), AgentMode.DO))
        outcome = manager.wait(scope, task.task_id, 10)
        assert outcome.result is not None and outcome.result.state is TaskState.COMPLETED
        assert "worktree task finished" in outcome.result.summary
        assert "status=completed" in outcome.result.summary
        assert "branch=newcode/subagent/" in outcome.result.summary
        assert "path=.newcode/worktrees/worker/" in outcome.result.summary
        assert "stderr" not in outcome.result.summary.lower()
        assert (repo / "README.md").read_text(encoding="utf-8") == "base contents\n"
        lease = worktrees.leases()[0]
        assert lease.state is WorktreeState.COMPLETED
        assert (lease.path / "README.md").read_text(encoding="utf-8") == "child contents\n"
        assert factory.created and len(provider.calls) == 2
        visible = [tool["function"]["name"] for tool in provider.calls[0]["tools"]]
        assert "run_command" not in visible
        assert parent.content == "parent history stays elsewhere"
    finally:
        manager.shutdown()


def test_copy_setup_finishes_before_provider_and_failure_does_not_fallback(tmp_path: Path):
    repo, worktrees, manager, scope, runner, factory, _registry = _runtime(
        tmp_path, _UnusedProvider(), policy=WorktreeSetupPolicy(copy_files=frozenset({"README.md"})),
    )
    (repo / "README.md").unlink()
    try:
        task = manager.start(scope, "must fail setup", payload=DefinitionTask(_definition()))
        outcome = manager.wait(scope, task.task_id, 10)
        assert outcome.result is not None and outcome.result.error_code == "subagent_worktree_setup_failed"
        assert factory.created == []
        lease = worktrees.leases()[0]
        assert lease.state is WorktreeState.FAILED
        assert lease.path.is_dir()  # Phase 5 dirty/clean removal policy is not available; preserve safely.
        assert (lease.path / "README.md").read_text(encoding="utf-8") == "base contents\n"
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    "fake_outcome,expected_state",
    [
        (WorkerResult(error_code="subagent_cancelled"), WorktreeState.CANCELLED),
        (WorkerResult(error_code="subagent_timeout"), WorktreeState.TIMED_OUT),
        (RuntimeError("secret path must not escape"), WorktreeState.FAILED),
    ],
)
def test_worktree_lease_is_released_on_cancel_timeout_and_exception(tmp_path: Path, monkeypatch, fake_outcome, expected_state):
    repo, worktrees, manager, scope, runner, _factory, _registry = _runtime(tmp_path, _UnusedProvider())

    def fake_run_child(*_args, **_kwargs):
        if isinstance(fake_outcome, BaseException):
            raise fake_outcome
        return fake_outcome

    monkeypatch.setattr(runner, "_run_child", fake_run_child)
    try:
        task = manager.start(scope, "controlled child outcome", payload=DefinitionTask(_definition()))
        result = manager.wait(scope, task.task_id, 10).result
        assert result is not None
        assert worktrees.leases()[0].state is expected_state
        if expected_state is WorktreeState.FAILED:
            assert result.error_code == "subagent_worktree_setup_failed"
            assert "secret path" not in result.summary
        assert worktrees.leases()[0].path.is_dir()
    finally:
        manager.shutdown()
