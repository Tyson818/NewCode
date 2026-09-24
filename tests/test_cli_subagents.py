from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
import threading

import pytest

from newcode import cli
from newcode.config import AppConfig
from newcode.hooks.types import HookLoadResult, HookNetworkPolicy
from newcode.permissions.types import PermissionMode
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.subagents.manager import SubAgentManager
from newcode.tools.types import ToolCall, ToolContext


@pytest.fixture(autouse=True)
def _isolate_hook_configuration(monkeypatch):
    monkeypatch.setattr(cli, "load_hook_rules", lambda _root: HookLoadResult((), HookNetworkPolicy()))


class ParentProvider:
    def __init__(self):
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), list(tools or ()), allow_tool_calls))
        index = len(self.calls) - 1
        if index == 0:
            assert any(item["function"]["name"] == "agent" for item in tools)
            yield ToolCallEvent([ToolCall(
                "start-definition", "agent", {
                    "operation": "start", "kind": "definition", "agent_name": "worker",
                    "task_prompt": "do the definition task", "execution": "foreground",
                },
            )])
        elif index == 1:
            task_id = _last_task_id(messages)
            yield ToolCallEvent([ToolCall(
                "wait-definition", "agent", {
                    "operation": "wait", "task_id": task_id, "wait_seconds": 2,
                },
            )])
        elif index == 2:
            task_id = _last_task_id(messages)
            yield ToolCallEvent([ToolCall(
                "collect-definition", "agent", {"operation": "collect", "task_id": task_id},
            )])
        elif index == 3:
            yield TextDelta("definition flow complete")
        elif index == 4:
            yield ToolCallEvent([ToolCall(
                "start-fork", "agent", {
                    "operation": "start", "kind": "fork", "task_prompt": "fork task",
                    "execution": "background", "allowlist": ["read_file"],
                },
            )])
        else:
            yield TextDelta("parent answer")


class ChildProvider:
    def __init__(self, label):
        self.label = label
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), list(tools or ()), allow_tool_calls))
        yield TextDelta(f"child-{self.label}-answer")


class BlockingChildProvider:
    def __init__(self, entered, release):
        self.entered = entered
        self.release = release

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.entered.set()
        self.release.wait(3)
        yield TextDelta("child stopped safely")


class FakeProviderFactory:
    default_model = "fixture-model"
    available_models = ("fixture-model",)

    def __init__(self):
        self.created = []

    def create(self, model, *, timeout_seconds):
        assert model == self.default_model
        assert 0 < timeout_seconds <= 300
        provider = ChildProvider(len(self.created) + 1)
        self.created.append(provider)
        return provider


def _last_task_id(messages):
    for message in reversed(messages):
        if message.role == "tool" and message.content:
            payload = json.loads(message.content)
            data = payload.get("data", {})
            task_id = data.get("task_id") or data.get("task", {}).get("task_id")
            if task_id:
                return task_id
    raise AssertionError("agent ToolResult did not contain task_id")


def _write_agent(workspace: Path):
    agents = workspace / ".newcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "worker.md").write_text(
        "---\n"
        "name: worker\n"
        "description: fixture child\n"
        "tools:\n"
        "  allow: [read_file]\n"
        "max_iterations: 3\n"
        "permission_mode: inherit\n"
        "---\n"
        "Private child SOP.\n",
        encoding="utf-8",
    )


def test_cli_definition_wait_collect_and_fork_background_notification(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    _write_agent(workspace)
    parent = ParentProvider()
    factory = FakeProviderFactory()
    session = ChatSession()
    output = StringIO()
    archive = cli.SessionArchive(workspace, sessions_root=home / ".newcode" / "sessions")
    values = iter(("definition request", "fork request", "next turn", "/exit"))

    code = cli.run_conversation(
        parent,
        session,
        tool_context=ToolContext(workspace),
        permission_mode=PermissionMode.TRUSTED,
        session_archive=archive,
        input_func=lambda _prompt: next(values),
        output=output,
        subagent_provider_factory=factory,
        agent_user_home=home,
    )

    assert code == 0
    assert len(factory.created) == 2
    assert all(client.calls for client in factory.created)
    assert any("child-1-answer" in (message.content or "") for message in session.messages)
    notices = [message for message in session.messages if "【子 Agent 后台任务通知】" in (message.content or "")]
    assert len(notices) == 1
    assert "child-2-answer" in (notices[0].content or "")
    assert len({message.content for message in notices}) == 1
    assert "Private child SOP" not in output.getvalue()
    assert len(list((home / ".newcode" / "sessions").glob("*.jsonl"))) == 1
    archived = archive.restore(session.session_id or "").session
    assert archived is not None
    assert all(not (message.content or "").startswith("subagent-") for message in archived.messages)


def test_cli_scope_generation_changes_on_clear_and_resume(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    sessions = home / ".newcode" / "sessions"
    workspace.mkdir()
    home.mkdir()
    archive = cli.SessionArchive(workspace, sessions_root=sessions)
    saved = ChatSession()
    saved.add_user_message("saved history")
    saved_id = archive.create(saved, suffix_factory=lambda: "a1z9")
    archive.checkpoint(saved)

    instances = []
    real_manager = SubAgentManager

    def capture_manager(worker):
        manager = real_manager(worker)
        instances.append(manager)
        return manager

    monkeypatch.setattr(cli, "SubAgentManager", capture_manager)
    factory = FakeProviderFactory()
    inputs = iter(("/clear", f"/resume {saved_id}", "/exit"))
    code = cli.run_conversation(
        ChildProvider("unused"),
        ChatSession(),
        tool_context=ToolContext(workspace),
        session_archive=archive,
        input_func=lambda _prompt: next(inputs),
        output=StringIO(),
        subagent_provider_factory=factory,
        agent_user_home=home,
    )
    assert code == 0
    assert len(instances) == 1
    assert len(instances[0]._active_scopes) == 0
    assert len(instances[0]._tasks) == 0
    # 新 scope 的 generation 由 Manager 单调递增；resume 只恢复会话消息，不恢复 task。
    assert instances[0]._next_generation == 3


def test_new_agent_tool_binds_new_scope_after_clear(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    _write_agent(workspace)
    archive = cli.SessionArchive(workspace, sessions_root=home / ".newcode" / "sessions")
    saved = ChatSession()
    saved.add_user_message("saved for resume")
    saved_id = archive.create(saved, suffix_factory=lambda: "r3s2")
    archive.checkpoint(saved)

    class Parent:
        calls = 0

        def stream_chat(self, messages, tools=None, allow_tool_calls=True):
            index = self.calls
            self.calls += 1
            if index == 0:
                yield TextDelta("before clear")
            elif index == 1:
                assert sum(item["function"]["name"] == "agent" for item in tools) == 1
                yield ToolCallEvent([ToolCall("after-clear", "agent", {
                    "operation": "start", "kind": "definition", "agent_name": "worker",
                    "task_prompt": "new scope", "execution": "foreground",
                })])
            elif index == 3:
                assert sum(item["function"]["name"] == "agent" for item in tools) == 1
                yield ToolCallEvent([ToolCall("after-resume", "agent", {
                    "operation": "start", "kind": "definition", "agent_name": "worker",
                    "task_prompt": "resumed scope", "execution": "foreground",
                })])
            else:
                yield TextDelta("after session switch")

    instances = []
    real_manager = SubAgentManager

    def capture_manager(worker):
        manager = real_manager(worker)
        instances.append(manager)
        return manager

    monkeypatch.setattr(cli, "SubAgentManager", capture_manager)
    values = iter(("before", "/clear", "after", f"/resume {saved_id}", "after resume", "/exit"))
    code = cli.run_conversation(
        Parent(), ChatSession(), tool_context=ToolContext(workspace),
        permission_mode=PermissionMode.TRUSTED, input_func=lambda _prompt: next(values),
        session_archive=archive,
        output=StringIO(), subagent_provider_factory=FakeProviderFactory(),
        agent_user_home=home,
    )
    assert code == 0
    manager = instances[0]
    assert manager._next_generation == 3
    assert len(manager._tasks) == 2
    assert {record.scope.generation for record in manager._tasks.values()} == {2, 3}


def test_cli_wait_timeout_then_cancel_and_clear_revoke_old_scope(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    entered, release = threading.Event(), threading.Event()

    class Parent:
        calls = 0

        def stream_chat(self, messages, tools=None, allow_tool_calls=True):
            index = self.calls
            self.calls += 1
            if index == 0:
                yield ToolCallEvent([ToolCall("start-bg", "agent", {
                    "operation": "start", "kind": "fork", "task_prompt": "blocking task",
                    "execution": "background", "allowlist": [],
                })])
            elif index == 1:
                yield ToolCallEvent([ToolCall("wait-bg", "agent", {
                    "operation": "wait", "task_id": _last_task_id(messages), "wait_seconds": 1,
                })])
            elif index == 2:
                task_id = _last_task_id(messages)
                assert entered.wait(1)
                threading.Timer(0.2, release.set).start()
                yield ToolCallEvent([ToolCall("cancel-bg", "agent", {
                    "operation": "cancel", "task_id": task_id,
                })])
            else:
                yield TextDelta("cancelled")

    class BlockingFactory(FakeProviderFactory):
        def create(self, _model, *, timeout_seconds):
            assert timeout_seconds > 0
            provider = BlockingChildProvider(entered, release)
            self.created.append(provider)
            return provider

    instances = []
    real_manager = SubAgentManager

    def capture_manager(worker):
        manager = real_manager(worker)
        instances.append(manager)
        return manager

    monkeypatch.setattr(cli, "SubAgentManager", capture_manager)
    values = iter(("launch", "/clear", "/exit"))
    provider = Parent()
    code = cli.run_conversation(
        provider,
        ChatSession(),
        tool_context=ToolContext(workspace),
        permission_mode=PermissionMode.TRUSTED,
        input_func=lambda _prompt: next(values),
        output=StringIO(),
        subagent_provider_factory=BlockingFactory(),
        agent_user_home=home,
    )
    release.set()
    assert code == 0
    manager = instances[0]
    assert manager._tasks
    assert all(record.state.value == "cancelled" for record in manager._tasks.values())
    assert all(record.scope_closed and record.cancel_event.is_set() for record in manager._tasks.values())
    assert all(not record.notification_pending and not record.summary for record in manager._tasks.values())


def test_cli_clear_cancels_running_child_and_discards_pending_notification(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    entered, release = threading.Event(), threading.Event()

    class Parent:
        calls = 0

        def stream_chat(self, messages, tools=None, allow_tool_calls=True):
            index = self.calls
            self.calls += 1
            if index == 0:
                yield ToolCallEvent([ToolCall("start-before-clear", "agent", {
                    "operation": "start", "kind": "fork", "task_prompt": "cancel on clear",
                    "execution": "background", "allowlist": [],
                })])
            else:
                yield TextDelta("parent done")

    class BlockingFactory(FakeProviderFactory):
        def create(self, _model, *, timeout_seconds):
            return BlockingChildProvider(entered, release)

    instances = []
    real_manager = SubAgentManager

    def capture_manager(worker):
        manager = real_manager(worker)
        instances.append(manager)
        return manager

    monkeypatch.setattr(cli, "SubAgentManager", capture_manager)
    values = iter(("launch", "/clear", "/exit"))
    read_count = 0

    def read(_prompt):
        nonlocal read_count
        read_count += 1
        if read_count == 2:
            assert entered.wait(1)
        if read_count == 3:
            release.set()
        return next(values)

    try:
        code = cli.run_conversation(
            Parent(), ChatSession(), tool_context=ToolContext(workspace),
            permission_mode=PermissionMode.TRUSTED, input_func=read,
            output=StringIO(), subagent_provider_factory=BlockingFactory(),
            agent_user_home=home,
        )
    finally:
        release.set()
    assert code == 0
    records = tuple(instances[0]._tasks.values())
    assert records and all(record.state.value == "cancelled" for record in records)
    assert all(record.scope_closed and record.cancel_event.is_set() for record in records)
    assert all(not record.notification_pending and not record.summary for record in records)


def test_cli_manager_shutdown_failure_does_not_skip_existing_cleanup(tmp_path, monkeypatch):
    order = []

    class FailingManager:
        def __init__(self, _worker):
            pass

        def open_session(self, session_id):
            return type("Scope", (), {"session_id": session_id, "generation": 1})()

        def close_session(self, _scope):
            order.append("scope_close")

        def shutdown(self, *, wait_timeout):
            order.append(("manager_shutdown", wait_timeout))
            raise RuntimeError("private detail")

    class Memory:
        store = None

        def shutdown(self):
            order.append("memory")

    class Context:
        def cleanup(self):
            order.append("context")

    monkeypatch.setattr(cli, "SubAgentManager", FailingManager)
    monkeypatch.setattr(cli, "_safe_hook_shutdown", lambda _actions: order.append("hook"))
    monkeypatch.setattr(cli, "_safe_memory_shutdown", lambda _memory: order.append("memory"))
    monkeypatch.setattr(cli, "_safe_context_cleanup", lambda _context: order.append("context"))
    code = cli.run_conversation(
        ChildProvider("unused"),
        ChatSession(session_id="cleanup"),
        tool_context=ToolContext(tmp_path),
        context_manager=Context(),
        memory_service=Memory(),
        input_func=lambda _prompt: "/exit",
        output=StringIO(),
        subagent_provider_factory=FakeProviderFactory(),
        agent_user_home=tmp_path,
    )
    assert code == 0
    assert order[0] == "scope_close"
    shutdown = order[1]
    assert isinstance(shutdown, tuple) and shutdown[0] == "manager_shutdown"
    assert 0 <= shutdown[1] <= 1
    assert order[2:] == ["hook", "memory", "context"]


def test_cli_provider_factory_only_supports_configured_model_and_creates_independent_clients():
    clients = []

    def client_factory(**kwargs):
        clients.append(kwargs)
        return object()

    factory = cli._CLIProviderFactory(
        AppConfig(model="configured-model"),
        "fixture-secret",
        client_factory=client_factory,
    )
    assert factory.available_models == ("configured-model",)
    first = factory.create("configured-model", timeout_seconds=300)
    second = factory.create("configured-model", timeout_seconds=2)
    assert first.client is not second.client
    assert clients[0]["timeout"] == 60
    assert clients[1]["timeout"] == 2
    with pytest.raises(RuntimeError, match="subagent_model_unavailable"):
        factory.create("undeclared-model", timeout_seconds=2)
    assert len(clients) == 2


def test_hook_subagent_placeholder_remains_unavailable_in_cli(tmp_path, monkeypatch):
    from newcode.hooks.types import HookAction, HookActionType, HookLoadResult, HookNetworkPolicy, HookRule, HookEvent, HookSource

    rules = (HookRule(
        id="no-subagent", event=HookEvent.TURN_START,
        action=HookAction(HookActionType.SUBAGENT, {}), source=HookSource.USER,
    ),)
    monkeypatch.setattr(cli, "load_hook_rules", lambda _root: HookLoadResult(rules, HookNetworkPolicy()))
    output, errors = StringIO(), StringIO()
    factory = FakeProviderFactory()
    code = cli.run_conversation(
        ChildProvider("unused"), ChatSession(session_id="hook-only"),
        tool_context=ToolContext(tmp_path), input_func=lambda _prompt: "/exit",
        output=output, error_output=errors, subagent_provider_factory=factory,
        agent_user_home=tmp_path,
    )
    assert code == 0
    assert factory.created == []
    assert "hook_subagent_not_available" not in output.getvalue() + errors.getvalue()


@pytest.mark.parametrize("input_value", [EOFError(), KeyboardInterrupt(), "/exit", RuntimeError("fixture failure")])
def test_manager_shutdown_runs_once_for_all_cli_exit_paths(tmp_path, monkeypatch, input_value):
    managers = []
    shutdowns = []
    real_manager = SubAgentManager

    class CaptureManager(real_manager):
        def __init__(self, worker):
            super().__init__(worker)
            managers.append(self)

        def shutdown(self, wait_timeout=1.0):
            shutdowns.append(wait_timeout)
            return super().shutdown(wait_timeout)

    monkeypatch.setattr(cli, "SubAgentManager", CaptureManager)

    def read(_prompt):
        if isinstance(input_value, BaseException):
            raise input_value
        return input_value

    try:
        result = cli.run_conversation(
            ChildProvider("unused"), ChatSession(session_id="all-exits"),
            tool_context=ToolContext(tmp_path), input_func=read, output=StringIO(),
            subagent_provider_factory=FakeProviderFactory(), agent_user_home=tmp_path,
        )
    except RuntimeError as exc:
        if str(exc) != "fixture failure":
            raise
    else:
        assert result == 0

    assert len(managers) == 1
    assert len(shutdowns) == 1
    assert 0 <= shutdowns[0] <= 1
    assert managers[0]._shutdown is True
