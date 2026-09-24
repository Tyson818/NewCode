from __future__ import annotations

from io import StringIO
from pathlib import Path

from newcode import cli
from newcode.session import ChatSession
from newcode.tools.types import ToolContext
from newcode.worktrees.manager import WorktreeManager


class UnusedProviderFactory:
    default_model = "fixture-model"
    available_models = ("fixture-model",)

    def create(self, *_args, **_kwargs):
        raise AssertionError("No child is started in this CLI lifecycle test")


class UnusedProvider:
    def stream_chat(self, *_args, **_kwargs):
        raise AssertionError("No model request is made before /exit")


def test_cli_owns_external_empty_hooks_directory_and_bounds_cleanup_failures(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    made = []
    cleanup_calls = []
    service_cleanup_order = []
    real_manager = WorktreeManager

    class TrackingManager(real_manager):
        def __init__(self, *, empty_hooks_dir, **kwargs):
            super().__init__(empty_hooks_dir=empty_hooks_dir, **kwargs)
            hooks = Path(empty_hooks_dir)
            made.append((self, hooks, hooks.is_dir() and not hooks.is_symlink() and not any(hooks.iterdir())))

        def cleanup_stale(self, *_args, **_kwargs):
            cleanup_calls.append("worktree_cleanup")
            raise RuntimeError("fixture cleanup failure")

    monkeypatch.setattr(cli, "WorktreeManager", TrackingManager)
    monkeypatch.setattr(cli, "_safe_hook_shutdown", lambda _actions: service_cleanup_order.append("hook"))
    monkeypatch.setattr(cli, "_safe_memory_shutdown", lambda _service: service_cleanup_order.append("memory"))
    monkeypatch.setattr(cli, "_safe_context_cleanup", lambda _context: service_cleanup_order.append("context"))
    result = cli.run_conversation(
        UnusedProvider(),
        ChatSession(),
        tool_context=ToolContext(workspace),
        input_func=lambda _prompt: "/exit",
        output=StringIO(),
        error_output=StringIO(),
        subagent_provider_factory=UnusedProviderFactory(),
        agent_user_home=home,
        memory_service=object(),
        context_manager=object(),
    )

    assert result == 0
    assert len(made) == 1
    manager, hooks, valid_when_created = made[0]
    assert hooks.is_absolute()
    assert valid_when_created
    assert hooks.is_dir() is False  # Temporary hooks root is released after Manager shutdown.
    assert hooks != workspace
    assert cleanup_calls == ["worktree_cleanup"]
    assert service_cleanup_order == ["hook", "memory", "context"]
    assert manager._git._hooks == hooks


def test_cli_clear_resume_and_exit_keep_old_subagent_scope_isolated(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    archive = cli.SessionArchive(workspace, sessions_root=home / ".newcode" / "sessions")
    saved = ChatSession()
    saved.add_user_message("saved session")
    saved_id = archive.create(saved, suffix_factory=lambda: "w4t7")
    archive.checkpoint(saved)
    values = iter(("/clear", f"/resume {saved_id}", "/exit"))

    result = cli.run_conversation(
        UnusedProvider(),
        ChatSession(),
        tool_context=ToolContext(workspace),
        session_archive=archive,
        input_func=lambda _prompt: next(values),
        output=StringIO(),
        error_output=StringIO(),
        subagent_provider_factory=UnusedProviderFactory(),
        agent_user_home=home,
    )

    assert result == 0
