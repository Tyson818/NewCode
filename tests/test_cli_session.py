from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from newcode import cli
from newcode.context.manager import ContextManager
from newcode.persistence import SessionArchive
from newcode.providers.base import ProviderError, TextDelta
from newcode.session import ChatSession
from newcode.tools.types import ToolContext


class Provider:
    def __init__(self, *, error: bool = False):
        self.error = error
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), tools, allow_tool_calls))
        if self.error:
            raise ProviderError("safe failure")
        yield TextDelta("answer")


class MemorySpy:
    def __init__(self, order=None):
        self.shutdown_calls = 0
        self.submits = 0
        self.order = order

    def submit(self, messages):
        self.submits += 1
        return True

    def shutdown(self):
        self.shutdown_calls += 1
        if self.order is not None:
            self.order.append("memory")
        return True


class OrderedArchive:
    def __init__(self, order, *, fail_checkpoint=False):
        self.order = order
        self.fail_checkpoint = fail_checkpoint

    def cleanup_stale(self, **kwargs):
        self.order.append("stale")

    def create(self, session):
        session.session_id = "20260922-083015-a1z9"

    def checkpoint(self, session):
        self.order.append("checkpoint")
        if self.fail_checkpoint:
            raise RuntimeError("archive failure")

    def list_recoverable(self):
        return ()

    def restore(self, session_id):
        return None


class OrderedContext:
    def __init__(self, order):
        self.order = order

    def prepare(self, _generator):
        return False

    def record_usage(self, _usage):
        return False

    def cleanup(self):
        self.order.append("context")


def _inputs(*values):
    iterator = iter(values)

    def read(prompt):
        value = next(iterator)
        if isinstance(value, BaseException):
            raise value
        return value

    return read


def _archive(tmp_path: Path) -> SessionArchive:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return SessionArchive(workspace, sessions_root=tmp_path / "home" / ".newcode" / "sessions")


def test_natural_turn_checkpoint_and_session_listing_are_safe(tmp_path: Path):
    archive = _archive(tmp_path)
    saved = ChatSession()
    saved.add_user_message("previous")
    saved_id = archive.create(saved, suffix_factory=lambda: "a1z9")
    archive.checkpoint(saved)
    current = ChatSession()
    output = StringIO()
    memory = MemorySpy()

    cli.run_conversation(
        Provider(),
        current,
        tool_context=ToolContext(tmp_path / "workspace"),
        session_archive=archive,
        memory_service=memory,
        input_func=_inputs("/sessions", "hello", "/exit"),
        output=output,
    )

    restored = archive.restore(current.session_id or "")
    assert saved_id in output.getvalue()
    assert "已归档会话" in output.getvalue()
    assert restored.session is not None
    assert [item.content for item in restored.session.messages] == ["hello", "answer"]
    assert memory.submits == 1
    assert memory.shutdown_calls == 1


def test_resume_restores_current_workspace_session_and_time_span_reminder(tmp_path: Path):
    archive = _archive(tmp_path)
    previous = ChatSession()
    previous.add_user_message("old context")
    old_time = datetime.now(timezone.utc) - timedelta(hours=25)
    session_id = archive.create(previous, now=old_time, suffix_factory=lambda: "a1z9")
    archive.checkpoint(previous, now=old_time)
    output = StringIO()
    provider = Provider()

    cli.run_conversation(
        provider,
        ChatSession(),
        tool_context=ToolContext(tmp_path / "workspace"),
        session_archive=archive,
        memory_service=MemorySpy(),
        input_func=_inputs(f"/resume {session_id}", "continue", "/exit"),
        output=output,
    )

    first_request = provider.calls[0][0]
    assert any(message.content == "old context" for message in first_request)
    assert "已恢复会话" in output.getvalue()
    assert "超过 24 小时" in output.getvalue()


def test_resume_rejects_invalid_and_cross_workspace_ids_without_leaking(tmp_path: Path):
    archive = _archive(tmp_path)
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    other = SessionArchive(other_workspace, sessions_root=archive.sessions_root)
    session_id = other.create(ChatSession(), suffix_factory=lambda: "a1z9")
    output = StringIO()

    cli.run_conversation(
        Provider(),
        ChatSession(),
        tool_context=ToolContext(tmp_path / "workspace"),
        session_archive=archive,
        memory_service=MemorySpy(),
        input_func=_inputs("/resume invalid", f"/resume {session_id}", "/exit"),
        output=output,
    )

    assert output.getvalue().count("会话不可恢复") == 2
    assert "other" not in output.getvalue()


def test_finally_order_and_archive_failure_isolation(tmp_path: Path):
    order: list[str] = []
    archive = OrderedArchive(order, fail_checkpoint=True)
    context = OrderedContext(order)
    memory = MemorySpy(order)

    cli.run_conversation(
        Provider(),
        ChatSession(),
        tool_context=ToolContext(tmp_path),
        session_archive=archive,
        memory_service=memory,
        context_manager=context,
        input_func=_inputs("/exit"),
        output=StringIO(),
    )

    assert order == ["stale", "checkpoint", "stale", "memory", "context"]


def test_eof_keyboard_interrupt_and_provider_error_all_cleanup(tmp_path: Path):
    for values, provider in [
        ((EOFError(),), Provider()),
        ((KeyboardInterrupt(),), Provider()),
        (("hello", "/exit"), Provider(error=True)),
    ]:
        order: list[str] = []
        cli.run_conversation(
            provider,
            ChatSession(),
            tool_context=ToolContext(tmp_path),
            session_archive=OrderedArchive(order),
            memory_service=MemorySpy(order),
            context_manager=OrderedContext(order),
            input_func=_inputs(*values),
            output=StringIO(),
            error_output=StringIO(),
        )
        assert order[-4:] == ["checkpoint", "stale", "memory", "context"]
