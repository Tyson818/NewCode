from __future__ import annotations

import time
from dataclasses import dataclass

from newcode.agent.scheduler import ToolBatch, ToolScheduler
from newcode.tools.registry import ToolRegistry, create_default_registry
from newcode.tools.types import JsonObject, ToolCall, ToolContext, ToolResult, ToolSpec


@dataclass
class DummyTool:
    name: str

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=f"{self.name} tool",
            parameters={"type": "object", "properties": {}},
        )

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        return ToolResult.success(self.name, arguments)


def call(name: str, call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments={"id": call_id})


def batch_names(batch: ToolBatch) -> list[str]:
    return [item.tool_call.name for item in batch.items]


def test_read_only_tools_share_parallel_batch():
    scheduler = ToolScheduler(create_default_registry())

    batches = scheduler.make_batches(
        [
            call("read_file", "call_1"),
            call("find_files", "call_2"),
            call("search_code", "call_3"),
        ]
    )

    assert len(batches) == 1
    assert batches[0].parallel is True
    assert batch_names(batches[0]) == ["read_file", "find_files", "search_code"]


def test_side_effect_tools_are_single_serial_batches():
    scheduler = ToolScheduler(create_default_registry())

    batches = scheduler.make_batches(
        [
            call("write_file", "call_1"),
            call("replace_in_file", "call_2"),
            call("run_command", "call_3"),
        ]
    )

    assert [batch.parallel for batch in batches] == [False, False, False]
    assert [batch_names(batch) for batch in batches] == [
        ["write_file"],
        ["replace_in_file"],
        ["run_command"],
    ]


def test_mixed_tools_are_split_in_original_order():
    scheduler = ToolScheduler(create_default_registry())

    batches = scheduler.make_batches(
        [
            call("read_file", "call_1"),
            call("find_files", "call_2"),
            call("write_file", "call_3"),
            call("search_code", "call_4"),
            call("run_command", "call_5"),
        ]
    )

    assert [(batch.parallel, batch_names(batch)) for batch in batches] == [
        (True, ["read_file", "find_files"]),
        (False, ["write_file"]),
        (True, ["search_code"]),
        (False, ["run_command"]),
    ]


def test_unknown_tool_is_not_batched_for_execution():
    scheduler = ToolScheduler(create_default_registry())

    batches = scheduler.make_batches(
        [
            call("read_file", "call_1"),
            call("missing_tool", "call_2"),
            call("find_files", "call_3"),
        ]
    )

    assert [(batch.parallel, batch_names(batch)) for batch in batches] == [
        (True, ["read_file"]),
        (True, ["find_files"]),
    ]


def test_parallel_read_only_results_are_returned_in_original_order():
    scheduler = ToolScheduler(create_default_registry())
    tool_calls = [
        call("read_file", "slow"),
        call("find_files", "fast"),
        call("search_code", "middle"),
    ]
    completion_order: list[str] = []

    def execute(tool_call: ToolCall) -> ToolResult:
        delays = {"slow": 0.03, "middle": 0.02, "fast": 0.01}
        time.sleep(delays[tool_call.id])
        completion_order.append(tool_call.id)
        return ToolResult.success(tool_call.name, tool_call.id)

    records = scheduler.execute(
        tool_calls,
        execute,
        lambda tool_call: ToolResult.failure(tool_call.name, "unknown_tool", "missing"),
    )

    assert completion_order == ["fast", "middle", "slow"]
    assert [record.tool_call.id for record in records] == ["slow", "fast", "middle"]
    assert [record.result.data for record in records] == ["slow", "fast", "middle"]


def test_unknown_tool_gets_result_without_executor_call():
    scheduler = ToolScheduler(create_default_registry())
    executed: list[str] = []

    records = scheduler.execute(
        [call("missing_tool", "call_1")],
        lambda tool_call: executed.append(tool_call.name) or ToolResult.success(
            tool_call.name
        ),
        lambda tool_call: ToolResult.failure(tool_call.name, "unknown_tool", "missing"),
    )

    assert executed == []
    assert records[0].result.ok is False
    assert records[0].result.error.code == "unknown_tool"


def test_scheduler_treats_unclassified_registered_tool_as_serial():
    registry = ToolRegistry()
    registry.register(DummyTool("custom_tool"))
    scheduler = ToolScheduler(registry)

    batches = scheduler.make_batches([call("custom_tool", "call_1")])

    assert batches == [
        ToolBatch(
            items=batches[0].items,
            parallel=False,
        )
    ]
