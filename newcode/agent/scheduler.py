from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from newcode.tools.registry import ToolRegistry
from newcode.tools.types import ToolCall, ToolResult


@dataclass(frozen=True)
class ToolExecutionItem:
    index: int
    tool_call: ToolCall


@dataclass(frozen=True)
class ToolBatch:
    items: list[ToolExecutionItem]
    parallel: bool


@dataclass(frozen=True)
class ToolExecutionRecord:
    index: int
    tool_call: ToolCall
    result: ToolResult


class ToolScheduler:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def make_batches(self, tool_calls: list[ToolCall]) -> list[ToolBatch]:
        batches: list[ToolBatch] = []
        read_only_items: list[ToolExecutionItem] = []

        for index, tool_call in enumerate(tool_calls):
            if self.registry.get(tool_call.name) is None:
                if read_only_items:
                    batches.append(ToolBatch(items=read_only_items, parallel=True))
                    read_only_items = []
                continue

            item = ToolExecutionItem(index=index, tool_call=tool_call)
            if self.registry.is_read_only(tool_call.name):
                read_only_items.append(item)
                continue

            if read_only_items:
                batches.append(ToolBatch(items=read_only_items, parallel=True))
                read_only_items = []
            batches.append(ToolBatch(items=[item], parallel=False))

        if read_only_items:
            batches.append(ToolBatch(items=read_only_items, parallel=True))

        return batches

    def execute(
        self,
        tool_calls: list[ToolCall],
        execute_call: Callable[[ToolCall], ToolResult],
        make_unknown_result: Callable[[ToolCall], ToolResult],
    ) -> list[ToolExecutionRecord]:
        batches = self.make_batches(tool_calls)
        known_indexes = {
            item.index
            for batch in batches
            for item in batch.items
        }
        records = [
            ToolExecutionRecord(
                index=index,
                tool_call=tool_call,
                result=make_unknown_result(tool_call),
            )
            for index, tool_call in enumerate(tool_calls)
            if index not in known_indexes
        ]

        for batch in batches:
            if batch.parallel and len(batch.items) > 1:
                records.extend(self._execute_parallel(batch, execute_call))
                continue

            for item in batch.items:
                records.append(
                    ToolExecutionRecord(
                        index=item.index,
                        tool_call=item.tool_call,
                        result=execute_call(item.tool_call),
                    )
                )

        return sorted(records, key=lambda record: record.index)

    def _execute_parallel(
        self,
        batch: ToolBatch,
        execute_call: Callable[[ToolCall], ToolResult],
    ) -> list[ToolExecutionRecord]:
        records: list[ToolExecutionRecord] = []
        with ThreadPoolExecutor(max_workers=len(batch.items)) as executor:
            futures = {
                executor.submit(execute_call, item.tool_call): item
                for item in batch.items
            }
            for future in as_completed(futures):
                item = futures[future]
                records.append(
                    ToolExecutionRecord(
                        index=item.index,
                        tool_call=item.tool_call,
                        result=future.result(),
                    )
                )
        return records
