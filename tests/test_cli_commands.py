from __future__ import annotations

from io import StringIO
from pathlib import Path

from newcode import cli
from newcode.agent.mode import AgentMode
from newcode.context.manager import ContextManager
from newcode.persistence import SessionArchive
from newcode.permissions.confirmer import DenyByDefaultConfirmer
from newcode.permissions.manager import PermissionManager
from newcode.permissions.types import PermissionMode
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolCall, ToolContext, ToolResult, ToolSpec


class Provider:
    def __init__(self):
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), tools, allow_tool_calls))
        yield TextDelta("answer")


class Memory:
    def __init__(self):
        self.submits = 0
        self.shutdowns = 0

    def submit(self, messages):
        self.submits += 1
        return True

    def shutdown(self):
        self.shutdowns += 1
        return True


class BatchProvider:
    def __init__(self, batches):
        self.batches = batches
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), tools, allow_tool_calls))
        yield from self.batches[len(self.calls) - 1]


class RecordingTool:
    def __init__(self, name):
        self.name = name
        self.calls = []

    @property
    def spec(self):
        return ToolSpec(self.name, "fake", {"type": "object"})

    def run(self, arguments, context):
        self.calls.append(dict(arguments))
        return ToolResult.success(self.name, arguments)


class ContextSpy:
    def __init__(self):
        self.prepares = 0
        self.usages = 0
        self.cleanups = 0

    def prepare(self, _generator):
        self.prepares += 1
        return False

    def record_usage(self, _usage):
        self.usages += 1
        return False

    def cleanup(self):
        self.cleanups += 1
        return True


def inputs(*values):
    iterator = iter(values)
    return lambda _prompt: next(iterator)


def archive(tmp_path: Path) -> SessionArchive:
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    return SessionArchive(root, sessions_root=tmp_path / "home" / ".newcode" / "sessions")


def test_unknown_slash_never_reaches_provider_and_help_is_local(tmp_path: Path):
    provider = Provider()
    output = StringIO()

    cli.run_conversation(
        provider,
        ChatSession(),
        tool_context=ToolContext(tmp_path),
        input_func=inputs("/missing secret-value", "/help", "/exit"),
        output=output,
    )

    assert provider.calls == []
    assert "未知命令。输入 /help" in output.getvalue()
    assert "/review" in output.getvalue()
    assert "secret-value" not in output.getvalue()


def test_only_plain_text_and_fixed_review_input_enter_agent_loop(tmp_path: Path, monkeypatch):
    provider = Provider()
    seen = []
    original_run = cli.AgentLoop.run

    def spy(self, user_input, *, mode=AgentMode.DO, cancel_flag=None):
        seen.append((user_input, mode, self.permission_manager))
        yield from original_run(self, user_input, mode=mode, cancel_flag=cancel_flag)

    monkeypatch.setattr(cli.AgentLoop, "run", spy)
    cli.run_conversation(
        provider,
        ChatSession(),
        tool_context=ToolContext(tmp_path),
        input_func=inputs("/status", "/review", "plain input", "/exit"),
        output=StringIO(),
    )

    assert [item[0] for item in seen] == [cli.REVIEW_AI_INPUT if hasattr(cli, "REVIEW_AI_INPUT") else "审查当前工作区未提交变更，说明风险、证据和建议，不擅自修改。", "plain input"]
    assert all(item[2] is not None for item in seen)


def test_plan_and_do_change_agent_mode_without_changing_permission_manager(tmp_path: Path, monkeypatch):
    provider = Provider()
    seen = []
    original_run = cli.AgentLoop.run

    def spy(self, user_input, *, mode=AgentMode.DO, cancel_flag=None):
        seen.append((mode, self.permission_manager.mode))
        yield from original_run(self, user_input, mode=mode, cancel_flag=cancel_flag)

    monkeypatch.setattr(cli.AgentLoop, "run", spy)
    cli.run_conversation(provider, ChatSession(), tool_context=ToolContext(tmp_path), input_func=inputs("/plan", "/review", "/do", "/review", "/exit"), output=StringIO())

    assert seen == [(AgentMode.PLAN, seen[0][1]), (AgentMode.DO, seen[1][1])]
    assert seen[0][1] == seen[1][1]


def test_clear_checkpoints_old_session_and_keeps_mode_memory_and_permission(tmp_path: Path, monkeypatch):
    stored = archive(tmp_path)
    session = ChatSession()
    memory = Memory()
    provider = Provider()
    old_context = ContextManager(session, tmp_path / "workspace")
    cleanup_calls = []
    old_context.cleanup = lambda: cleanup_calls.append("old") or True
    checkpoints = []
    original_checkpoint = stored.checkpoint
    original_run = cli.AgentLoop.run
    observed = []

    def checkpoint(value):
        checkpoints.append(value.session_id)
        return original_checkpoint(value)

    def spy(self, user_input, *, mode=AgentMode.DO, cancel_flag=None):
        observed.append((mode, self.memory_service, self.permission_manager))
        yield from original_run(self, user_input, mode=mode, cancel_flag=cancel_flag)

    monkeypatch.setattr(stored, "checkpoint", checkpoint)
    monkeypatch.setattr(cli.AgentLoop, "run", spy)
    cli.run_conversation(
        provider,
        session,
        tool_context=ToolContext(tmp_path / "workspace"),
        context_manager=old_context,
        session_archive=stored,
        memory_service=memory,
        input_func=inputs("/plan", "/clear", "/review", "/exit"),
        output=StringIO(),
    )

    assert checkpoints[0] == session.session_id
    assert cleanup_calls == ["old"]
    assert observed[0][0] is AgentMode.PLAN
    assert observed[0][1] is memory
    assert observed[0][2] is not None
    assert memory.shutdowns == 1
    assert provider.calls


def test_review_in_plan_mode_keeps_mcp_hidden_and_disallowed(tmp_path: Path):
    mcp_tool = RecordingTool("mcp__server__work__123456789abc")
    registry = ToolRegistry()
    registry.register(mcp_tool, read_only=False, do_visible=True)
    provider = BatchProvider([[ToolCallEvent([ToolCall("call_1", mcp_tool.name, {})])]])

    cli.run_conversation(
        provider,
        ChatSession(),
        registry=registry,
        tool_context=ToolContext(tmp_path),
        permission_manager=PermissionManager(mode=PermissionMode.TRUSTED),
        input_func=inputs("/plan", "/review", "/exit"),
        output=StringIO(),
    )

    tool_names = {item["function"]["name"] for item in provider.calls[0][1]}
    assert mcp_tool.name not in tool_names
    assert mcp_tool.calls == []


def test_review_keeps_permission_denial_and_tool_result_feedback(tmp_path: Path):
    tool = RecordingTool("write_file")
    registry = ToolRegistry()
    registry.register(tool)
    provider = BatchProvider(
        [
            [ToolCallEvent([ToolCall("call_1", "write_file", {"path": "note.txt", "content": "x"})])],
            [TextDelta("safe answer")],
        ]
    )

    cli.run_conversation(
        provider,
        ChatSession(),
        registry=registry,
        tool_context=ToolContext(tmp_path),
        permission_manager=PermissionManager(confirmer=DenyByDefaultConfirmer()),
        input_func=inputs("/review", "/exit"),
        output=StringIO(),
    )

    assert tool.calls == []
    assert any(message.role == "tool" and "permission_denied" in (message.content or "") for message in provider.calls[1][0])


def test_review_uses_existing_context_prepare_and_natural_memory_submission(tmp_path: Path):
    provider = BatchProvider([[TextDelta("review answer")]])
    context = ContextSpy()
    memory = Memory()

    cli.run_conversation(
        provider,
        ChatSession(),
        tool_context=ToolContext(tmp_path),
        context_manager=context,
        memory_service=memory,
        input_func=inputs("/review", "/exit"),
        output=StringIO(),
    )

    assert context.prepares == 1
    assert context.usages == 1
    assert context.cleanups == 1
    assert memory.submits == 1
    assert memory.shutdowns == 1
