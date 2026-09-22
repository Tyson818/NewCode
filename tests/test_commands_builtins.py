from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from newcode.commands.builtins import CommandRuntime, REVIEW_AI_INPUT, create_builtin_registry
from newcode.commands.dispatcher import CommandDispatcher
from newcode.commands.types import CommandOutcomeKind, CommandParseKind
from newcode.commands.ui import FakeUIControl


@dataclass(frozen=True)
class Summary:
    session_id: str = "20260922-120000-a1z9"
    title: str = "已归档会话"
    updated_at: datetime = datetime(2026, 9, 22, tzinfo=timezone.utc)
    message_count: int = 2


@dataclass(frozen=True)
class Note:
    id: str
    scope: object
    category: object
    updated_at: datetime


class Value:
    def __init__(self, value: str):
        self.value = value


def runtime(registry, *, compact: str = "compacted"):
    calls: list[str] = []

    def compact_call():
        calls.append("compact")
        return compact

    def memories(scope):
        calls.append(f"memory:{scope}")
        return (Note("note-a", Value("user"), Value("用户偏好"), datetime(2026, 9, 22, tzinfo=timezone.utc)),)

    return (
        CommandRuntime(
            registry=registry,
            manual_compact=compact_call,
            list_sessions=lambda: (Summary(),),
            list_memory=memories,
            permission_summary=lambda: "权限模式：default；会话规则：0 条；安全诊断：0 项。",
            status_summary=lambda: "模式：do；会话：已归档；MCP 已配置 0 个服务。",
        ),
        calls,
    )


def execute(text: str, *, compact: str = "compacted"):
    registry = create_builtin_registry()
    subject = CommandDispatcher(registry)
    ui = FakeUIControl()
    parsed = subject.dispatch_input(text, ui)
    assert parsed.kind is CommandParseKind.COMMAND
    assert parsed.command is not None
    value, calls = runtime(registry, compact=compact)
    return subject.execute(parsed.command, value, ui), ui, calls, registry


def test_registry_contains_exactly_ten_builtin_commands_and_compatible_aliases():
    registry = create_builtin_registry()

    assert [item.name for item in registry.visible_definitions()] == [
        "/clear", "/compact", "/do", "/help", "/memory", "/permission", "/plan", "/review", "/session", "/status"
    ]
    assert registry.get("/sessions") is registry.get("/session")
    assert registry.get("/resume") is registry.get("/session")
    assert registry.get("/exit") is None


def test_help_compact_memory_permission_and_status_use_only_runtime_callbacks():
    outcome, ui, calls, _ = execute("/help")
    assert outcome.kind is CommandOutcomeKind.HANDLED
    assert len(ui.help_entries) == 1

    outcome, ui, calls, _ = execute("/compact", compact="no_history")
    assert outcome.kind is CommandOutcomeKind.HANDLED
    assert calls == ["compact"]
    assert ui.infos == ["没有可压缩的历史。"]

    outcome, ui, calls, _ = execute("/memory USER")
    assert outcome.kind is CommandOutcomeKind.HANDLED
    assert calls == ["memory:user"]
    assert "note-a | user | 用户偏好" in ui.infos[0]

    outcome, ui, _, _ = execute("/permission")
    assert outcome.kind is CommandOutcomeKind.HANDLED
    assert "权限模式：default" in ui.infos[0]

    outcome, ui, _, _ = execute("/status")
    assert outcome.kind is CommandOutcomeKind.HANDLED
    assert "MCP 已配置" in ui.infos[0]


def test_session_aliases_map_to_list_and_resume_outcomes():
    listed, ui, _, _ = execute("/sessions")
    assert listed.kind is CommandOutcomeKind.HANDLED
    assert "20260922-120000-a1z9" in ui.infos[0]

    resumed, _, _, _ = execute("/resume 20260922-120000-a1z9")
    assert resumed.kind is CommandOutcomeKind.RESUME_SESSION
    assert resumed.session_id == "20260922-120000-a1z9"

    canonical, _, _, _ = execute("/session resume 20260922-120000-a1z9")
    assert canonical == resumed


def test_clear_plan_do_and_review_are_declarative_and_review_is_fixed():
    cleared, _, _, _ = execute("/clear")
    assert cleared.kind is CommandOutcomeKind.CLEAR_SESSION

    planned, _, _, _ = execute("/plan")
    doing, _, _, _ = execute("/do")
    assert planned.kind is CommandOutcomeKind.MODE_CHANGE and planned.mode == "plan"
    assert doing.kind is CommandOutcomeKind.MODE_CHANGE and doing.mode == "do"

    reviewed, _, _, _ = execute("/review")
    assert reviewed.kind is CommandOutcomeKind.AI_INPUT
    assert reviewed.ai_input == REVIEW_AI_INPUT


def test_invalid_arguments_are_safe_and_do_not_call_runtime():
    outcome, ui, calls, _ = execute("/review extra")

    assert outcome.kind is CommandOutcomeKind.HANDLED
    assert calls == []
    assert ui.errors == [("command_invalid_arguments", "用法：/review")]
