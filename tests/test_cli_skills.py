from __future__ import annotations

from io import StringIO
from pathlib import Path

from newcode import cli
from newcode.commands.builtins import REVIEW_AI_INPUT
from newcode.permissions.manager import PermissionManager
from newcode.permissions.confirmer import DenyByDefaultConfirmer
from newcode.permissions.types import ConfirmationResult, ConfirmationScope
from newcode.persistence import SessionArchive
from newcode.providers.base import TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.tools.registry import create_default_registry
from newcode.tools.types import ToolCall, ToolContext


class Provider:
    def __init__(self, batches):
        self.batches = batches
        self.calls = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append((list(messages), list(tools or [])))
        yield from self.batches[len(self.calls) - 1]


class Allow:
    def confirm(self, request, decision):
        return ConfirmationResult(True, ConfirmationScope.ONCE, "allow")


class FirstAllowThenDeny:
    def __init__(self):
        self.calls = 0

    def confirm(self, request, decision):
        self.calls += 1
        return ConfirmationResult(self.calls == 1, ConfirmationScope.ONCE, "controlled")


def _write_skill(workspace: Path, tool: str = "read_file") -> None:
    path = workspace / ".newcode" / "skills" / "demo.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: demo\ndescription: Demo work.\ntools:\n- {tool}\nmode: shared\n---\n\ndemo SOP\n", encoding="utf-8")


def _inputs(*items):
    values = iter(items)
    return lambda _prompt: next(values)


def _load_call() -> ToolCall:
    return ToolCall("load", "load_skill", {"name": "demo", "parameters": {}})


def test_cli_shared_overlay_reuses_main_session_and_static_review_remains_fixed(tmp_path: Path):
    _write_skill(tmp_path)
    provider = Provider([[ToolCallEvent([_load_call()])], [TextDelta("loaded")], [TextDelta("shared done")], [TextDelta("review done")]])
    session = ChatSession()

    cli.run_conversation(
        provider,
        session,
        registry=create_default_registry(),
        tool_context=ToolContext(tmp_path),
        permission_manager=PermissionManager(confirmer=Allow()),
        input_func=_inputs("load demo", "/demo", "/review", "/exit"),
        output=StringIO(),
    )

    assert any(message.content and "执行已激活的受控 Skill：demo" in message.content for message in session.messages)
    assert any(message.content == "shared done" for message in session.messages)
    assert provider.calls[2][0][-1].content.startswith("执行已激活的受控 Skill：demo")
    assert provider.calls[3][0][-1].content == REVIEW_AI_INPUT


def test_clear_and_resume_remove_overlay_state(tmp_path: Path):
    _write_skill(tmp_path)
    provider = Provider([[ToolCallEvent([_load_call()])], [TextDelta("loaded")]])
    output = StringIO()
    cli.run_conversation(
        provider,
        ChatSession(),
        registry=create_default_registry(),
        tool_context=ToolContext(tmp_path),
        permission_manager=PermissionManager(confirmer=Allow()),
        input_func=_inputs("load demo", "/clear", "/demo", "/exit"),
        output=output,
    )
    assert len(provider.calls) == 2
    assert "未知命令" in output.getvalue()


def test_exit_and_input_interrupt_after_activation_clear_overlay_state(tmp_path: Path, monkeypatch):
    _write_skill(tmp_path)
    created = []
    original = cli.ActiveSkillState

    class TrackingState(original):
        def __init__(self):
            super().__init__()
            self.clears = 0
            created.append(self)

        def clear(self):
            self.clears += 1
            super().clear()

    monkeypatch.setattr(cli, "ActiveSkillState", TrackingState)
    for terminal in ("/exit", EOFError(), KeyboardInterrupt()):
        provider = Provider([[ToolCallEvent([_load_call()])], [TextDelta("loaded")]])
        values = iter(("load demo", terminal))

        def read(_prompt):
            value = next(values)
            if isinstance(value, BaseException):
                raise value
            return value

        cli.run_conversation(
            provider,
            ChatSession(),
            registry=create_default_registry(),
            tool_context=ToolContext(tmp_path),
            permission_manager=PermissionManager(confirmer=Allow()),
            input_func=read,
            output=StringIO(),
        )
    assert len(created) == 3
    assert all(state.clears >= 1 and state.activations == () for state in created)


def test_shared_overlay_permission_denial_is_returned_through_main_history(tmp_path: Path):
    _write_skill(tmp_path, tool="write_file")
    provider = Provider(
        [
            [ToolCallEvent([_load_call()])],
            [TextDelta("loaded")],
            [ToolCallEvent([ToolCall("write", "write_file", {"path": "missing.txt", "content": "x"})])],
            [TextDelta("shared safe answer")],
        ]
    )
    session = ChatSession()
    cli.run_conversation(
        provider,
        session,
        registry=create_default_registry(),
        tool_context=ToolContext(tmp_path),
        permission_manager=PermissionManager(confirmer=FirstAllowThenDeny()),
        input_func=_inputs("load demo", "/demo", "/exit"),
        output=StringIO(),
    )

    assert any(message.role == "tool" and "permission_denied" in (message.content or "") for message in session.messages)
    assert any(message.content == "shared safe answer" for message in session.messages)

    archive = SessionArchive(tmp_path, sessions_root=tmp_path / "home" / ".newcode" / "sessions")
    saved = ChatSession()
    saved_id = archive.create(saved, suffix_factory=lambda: "a1z9")
    provider = Provider([[ToolCallEvent([_load_call()])], [TextDelta("loaded")]])
    output = StringIO()
    cli.run_conversation(
        provider,
        ChatSession(),
        registry=create_default_registry(),
        tool_context=ToolContext(tmp_path),
        permission_manager=PermissionManager(confirmer=Allow()),
        session_archive=archive,
        input_func=_inputs("load demo", f"/resume {saved_id}", "/demo", "/exit"),
        output=output,
    )
    assert len(provider.calls) == 2
    assert "未知命令" in output.getvalue()
