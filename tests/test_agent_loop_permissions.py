from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from newcode.agent import (
    AgentFinalAnswer,
    AgentLoop,
    AgentLoopConfig,
    AgentStopped,
    AgentToolError,
    AgentToolResult,
    StopReason,
)
from newcode.agent.mode import AgentMode
from newcode.permissions.manager import PermissionManager
from newcode.permissions.rules import parse_permission_rules_document
from newcode.permissions.types import (
    ConfirmationResult,
    ConfirmationScope,
    PermissionDecisionValue,
    PermissionLayer,
    PermissionMode,
)
from newcode.providers.base import ProviderError, ProviderEvent, TextDelta, ToolCallEvent
from newcode.session import ChatSession
from newcode.tools.registry import ToolRegistry
from newcode.tools.types import JsonObject, ToolCall, ToolContext, ToolResult, ToolSpec


class FakeProvider:
    def __init__(self, batches: list[Iterable[ProviderEvent]]):
        self.batches = batches
        self.calls: list[dict[str, object]] = []

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "allow_tool_calls": allow_tool_calls,
            }
        )
        yield from self.batches[len(self.calls) - 1]


class ImmediateErrorProvider:
    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        raise ProviderError("provider failed")


class FakeConfirmer:
    def __init__(self, results: list[ConfirmationResult]):
        self.results = list(results)
        self.calls: list[tuple[str, str | None]] = []

    def confirm(self, request, decision):
        self.calls.append(
            (
                request.tool_name,
                request.normalized_args.get("relative_path")
                or request.normalized_args.get("command"),
            )
        )
        return self.results.pop(0)


@dataclass
class RecordingTool:
    name: str
    ok: bool = True
    calls: list[JsonObject] | None = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=f"{self.name} fake tool",
            parameters={"type": "object", "properties": {}},
        )

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        assert self.calls is not None
        self.calls.append(dict(arguments))
        if self.ok:
            return ToolResult.success(self.name, {"arguments": arguments})
        return ToolResult.failure(self.name, "fake_error", "tool failed")


def tool_call(name: str, call_id: str, arguments: dict[str, object] | None = None) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments or {},
        raw_arguments=json.dumps(arguments or {}),
    )


def make_registry(*tools: RecordingTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def make_loop(
    provider,
    tmp_path,
    *,
    registry: ToolRegistry,
    session: ChatSession | None = None,
    permission_manager: PermissionManager | None = None,
    config: AgentLoopConfig | None = None,
) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        session=session or ChatSession(),
        registry=registry,
        tool_context=ToolContext(workspace_root=tmp_path),
        permission_manager=permission_manager,
        config=config,
    )


def allow_manager() -> PermissionManager:
    return PermissionManager(mode=PermissionMode.TRUSTED)


def deny_rule_manager() -> PermissionManager:
    return PermissionManager(
        mode=PermissionMode.TRUSTED,
        project_rules=parse_permission_rules_document(
            {
                "rules": [
                    {
                        "id": "deny_readme",
                        "tool": "read_file",
                        "match": {"path": "README.md"},
                        "action": "deny",
                        "reason": "blocked by test rule",
                        "risk_level": "high",
                    }
                ]
            },
            source=PermissionLayer.PROJECT_RULES,
        ),
    )


def tool_messages(session: ChatSession):
    return [message for message in session.messages if message.role == "tool"]


def tool_errors(events):
    return [event for event in events if isinstance(event, AgentToolError)]


def stopped_events(events):
    return [event for event in events if isinstance(event, AgentStopped)]


def tool_payload(message):
    return json.loads(message.content or "{}")


def test_permission_deny_does_not_execute_tool_and_continues(tmp_path):
    tool = RecordingTool("read_file")
    session = ChatSession()
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call("read_file", "call_1", {"path": "README.md"})])],
            [TextDelta("adjusted")],
        ]
    )
    loop = make_loop(
        provider,
        tmp_path,
        registry=make_registry(tool),
        session=session,
        permission_manager=deny_rule_manager(),
    )

    events = list(loop.run("read"))

    assert tool.calls == []
    assert events[-1] == AgentFinalAnswer("adjusted")
    assert len(provider.calls) == 2
    assert tool_errors(events)[0].code == "permission_denied"
    payload = tool_payload(tool_messages(session)[0])
    assert payload["error"]["code"] == "permission_denied"
    assert payload["error"]["details"]["permission_layer"] == "project_rules"


def test_confirmation_refused_does_not_execute_tool(tmp_path):
    tool = RecordingTool("write_file")
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call("write_file", "call_1", {"path": "notes.txt", "content": "x"})])],
            [TextDelta("retry")],
        ]
    )
    manager = PermissionManager(
        mode=PermissionMode.DEFAULT,
        confirmer=FakeConfirmer(
            [ConfirmationResult(False, ConfirmationScope.ONCE, "refused")]
        ),
    )
    loop = make_loop(
        provider,
        tmp_path,
        registry=make_registry(tool),
        permission_manager=manager,
    )

    events = list(loop.run("write"))

    assert tool.calls == []
    assert events[-1] == AgentFinalAnswer("retry")
    assert tool_errors(events)[0].code == "permission_denied"
    assert tool_errors(events)[0].details["permission_layer"] == "hitl_confirmation"


def test_permission_allow_executes_tool(tmp_path):
    tool = RecordingTool("write_file")
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call("write_file", "call_1", {"path": "notes.txt", "content": "x"})])],
            [TextDelta("done")],
        ]
    )
    loop = make_loop(
        provider,
        tmp_path,
        registry=make_registry(tool),
        permission_manager=allow_manager(),
    )

    events = list(loop.run("write"))

    assert len(tool.calls or []) == 1
    assert any(isinstance(event, AgentToolResult) for event in events)
    assert events[-1] == AgentFinalAnswer("done")


def test_confirmation_allowed_once_executes_tool_without_session_rule(tmp_path):
    tool = RecordingTool("write_file")
    confirmer = FakeConfirmer(
        [ConfirmationResult(True, ConfirmationScope.ONCE, "allow once")]
    )
    manager = PermissionManager(mode=PermissionMode.DEFAULT, confirmer=confirmer)
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call("write_file", "call_1", {"path": "notes.txt", "content": "x"})])],
            [TextDelta("done")],
        ]
    )

    events = list(
        make_loop(
            provider,
            tmp_path,
            registry=make_registry(tool),
            permission_manager=manager,
        ).run("write")
    )

    assert len(tool.calls or []) == 1
    assert len(confirmer.calls) == 1
    assert len(manager.session_rules.rules) == 0
    assert events[-1] == AgentFinalAnswer("done")


def test_permission_denied_observation_is_sent_to_next_provider_request(tmp_path):
    tool = RecordingTool("write_file")
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call("write_file", "call_1", {"path": "notes.txt", "content": "x"})])],
            [TextDelta("saw denial")],
        ]
    )
    loop = make_loop(
        provider,
        tmp_path,
        registry=make_registry(tool),
        permission_manager=PermissionManager(mode=PermissionMode.DEFAULT),
    )

    events = list(loop.run("write"))

    assert tool.calls == []
    assert events[-1] == AgentFinalAnswer("saw denial")
    next_messages = provider.calls[1]["messages"]
    assert next_messages[-1].role == "tool"
    assert "permission_denied" in (next_messages[-1].content or "")


def test_multiple_confirmations_are_serial_in_model_order(tmp_path):
    tool = RecordingTool("write_file")
    confirmer = FakeConfirmer(
        [
            ConfirmationResult(True, ConfirmationScope.ONCE, "first"),
            ConfirmationResult(True, ConfirmationScope.ONCE, "second"),
        ]
    )
    provider = FakeProvider(
        [
            [
                ToolCallEvent(
                    [
                        tool_call("write_file", "call_1", {"path": "a.txt", "content": "a"}),
                        tool_call("write_file", "call_2", {"path": "b.txt", "content": "b"}),
                    ]
                )
            ],
            [TextDelta("done")],
        ]
    )

    list(
        make_loop(
            provider,
            tmp_path,
            registry=make_registry(tool),
            permission_manager=PermissionManager(
                mode=PermissionMode.DEFAULT,
                confirmer=confirmer,
            ),
        ).run("write twice")
    )

    assert confirmer.calls == [("write_file", "a.txt"), ("write_file", "b.txt")]
    assert [call["path"] for call in (tool.calls or [])] == ["a.txt", "b.txt"]


def test_mixed_allow_and_deny_results_keep_model_order(tmp_path):
    read_tool = RecordingTool("read_file")
    write_tool = RecordingTool("write_file")
    session = ChatSession()
    provider = FakeProvider(
        [
            [
                ToolCallEvent(
                    [
                        tool_call("write_file", "call_1", {"path": "a.txt", "content": "a"}),
                        tool_call("read_file", "call_2", {"path": "README.md"}),
                        tool_call("write_file", "call_3", {"path": "b.txt", "content": "b"}),
                    ]
                )
            ],
            [TextDelta("done")],
        ]
    )

    events = list(
        make_loop(
            provider,
            tmp_path,
            registry=make_registry(read_tool, write_tool),
            session=session,
            permission_manager=PermissionManager(mode=PermissionMode.DEFAULT),
        ).run("mixed")
    )

    assert write_tool.calls == []
    assert len(read_tool.calls or []) == 1
    assert events[-1] == AgentFinalAnswer("done")
    assert [message.tool_call_id for message in tool_messages(session)] == [
        "call_1",
        "call_2",
        "call_3",
    ]


def test_scheduler_behavior_is_preserved_for_allowed_tools(tmp_path):
    read_tool = RecordingTool("read_file")
    find_tool = RecordingTool("find_files")
    write_tool = RecordingTool("write_file")
    search_tool = RecordingTool("search_code")
    session = ChatSession()
    provider = FakeProvider(
        [
            [
                ToolCallEvent(
                    [
                        tool_call("read_file", "call_1", {"path": "README.md"}),
                        tool_call("find_files", "call_2", {"pattern": "*.py"}),
                        tool_call("write_file", "call_3", {"path": "notes.txt", "content": "x"}),
                        tool_call("search_code", "call_4", {"query": "AgentLoop"}),
                    ]
                )
            ],
            [TextDelta("done")],
        ]
    )

    list(
        make_loop(
            provider,
            tmp_path,
            registry=make_registry(read_tool, find_tool, write_tool, search_tool),
            session=session,
            permission_manager=allow_manager(),
        ).run("tools")
    )

    assert [len(tool.calls or []) for tool in (read_tool, find_tool, write_tool, search_tool)] == [
        1,
        1,
        1,
        1,
    ]
    assert [message.tool_call_id for message in tool_messages(session)] == [
        "call_1",
        "call_2",
        "call_3",
        "call_4",
    ]


def test_unknown_tool_behavior_is_not_replaced_by_permissions(tmp_path):
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call("missing_tool", "call_1")])],
        ]
    )
    loop = make_loop(
        provider,
        tmp_path,
        registry=make_registry(),
        permission_manager=PermissionManager(mode=PermissionMode.DEFAULT),
        config=AgentLoopConfig(unknown_tool_threshold=1),
    )

    events = list(loop.run("unknown"))

    assert tool_errors(events)[0].code == "unknown_tool"
    assert stopped_events(events)[-1].reason == StopReason.UNKNOWN_TOOL_LIMIT


def test_disallowed_tool_behavior_still_precedes_permissions(tmp_path):
    write_tool = RecordingTool("write_file")
    provider = FakeProvider(
        [[ToolCallEvent([tool_call("write_file", "call_1", {"path": "notes.txt"})])]]
    )
    loop = make_loop(
        provider,
        tmp_path,
        registry=make_registry(write_tool),
        permission_manager=allow_manager(),
    )

    events = list(loop.run("plan", mode=AgentMode.PLAN))

    assert write_tool.calls == []
    assert tool_errors(events)[0].code == "disallowed_tool"
    assert stopped_events(events)[-1].reason == StopReason.DISALLOWED_TOOL_CALL


def test_provider_error_behavior_is_unchanged(tmp_path):
    loop = make_loop(
        ImmediateErrorProvider(),
        tmp_path,
        registry=make_registry(RecordingTool("read_file")),
        permission_manager=allow_manager(),
    )

    events = list(loop.run("fail"))

    assert stopped_events(events)[-1].reason == StopReason.PROVIDER_ERROR


def test_tool_error_threshold_still_applies_to_permission_denied(tmp_path):
    tool = RecordingTool("write_file")
    provider = FakeProvider(
        [
            [ToolCallEvent([tool_call("write_file", "call_1", {"path": "a.txt", "content": "a"})])],
        ]
    )
    loop = make_loop(
        provider,
        tmp_path,
        registry=make_registry(tool),
        permission_manager=PermissionManager(mode=PermissionMode.DEFAULT),
        config=AgentLoopConfig(tool_error_threshold=1),
    )

    events = list(loop.run("write"))

    assert tool.calls == []
    assert tool_errors(events)[0].code == "permission_denied"
    assert stopped_events(events)[-1].reason == StopReason.TOOL_ERROR_LIMIT


def test_plan_mode_side_effect_tool_cannot_be_allowed_by_permission_mode(tmp_path):
    write_tool = RecordingTool("write_file")
    provider = FakeProvider(
        [[ToolCallEvent([tool_call("write_file", "call_1", {"path": "notes.txt"})])]]
    )

    events = list(
        make_loop(
            provider,
            tmp_path,
            registry=make_registry(write_tool),
            permission_manager=allow_manager(),
        ).run("plan write", mode=AgentMode.PLAN)
    )

    assert write_tool.calls == []
    assert tool_errors(events)[0].code == "disallowed_tool"
