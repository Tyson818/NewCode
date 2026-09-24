"""唯一静态 `agent` 工具：参数校验、受控 Manager 操作，不执行 child。"""

from __future__ import annotations

from collections.abc import Callable

from newcode.agent.mode import AgentMode
from newcode.context.redaction import redact_text
from newcode.subagents.manager import SubAgentManager
from newcode.subagents.runner import DefinitionTask, ForkTask, capture_fork_snapshot
from newcode.subagents.types import (
    AgentCatalog,
    MAX_TASK_SUMMARY_CHARS,
    SessionScope,
    SubAgentManagerError,
    TaskExecution,
    TASK_ERROR_CODES,
)
from newcode.tools.types import ToolContext, ToolFailure, ToolResult, ToolSpec


_INVALID = "subagent_invalid_request"
_ALLOWED_FIELDS = {
    "operation", "kind", "task_prompt", "execution", "agent_name", "model_override",
    "allowlist", "task_id", "wait_seconds",
}
_OPERATIONS = frozenset({"start", "status", "wait", "background", "cancel", "collect"})
_SAFE_MANAGER_CODES = TASK_ERROR_CODES | {"subagent_result_not_ready"}


class AgentTool:
    """固定 schema 工具；具体 Definition/Fork worker 由 Manager 的 callback 执行。"""

    def __init__(
        self,
        *,
        manager: SubAgentManager,
        scope: SessionScope,
        catalog: AgentCatalog,
        snapshot_provider: Callable[[], tuple],
        mode_provider: Callable[[], AgentMode],
    ) -> None:
        self.manager = manager
        self.scope = scope
        self.catalog = catalog
        self.snapshot_provider = snapshot_provider
        self.mode_provider = mode_provider

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="agent",
            description="启动或管理受限的子 Agent 任务。",
            parameters={
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": sorted(_OPERATIONS)},
                    "kind": {"type": "string", "enum": ["definition", "fork"]},
                    "task_prompt": {"type": "string", "minLength": 1},
                    "execution": {"type": "string", "enum": ["foreground", "background"]},
                    "agent_name": {"type": "string"},
                    "model_override": {"type": "string"},
                    "allowlist": {"type": "array", "items": {"type": "string"}},
                    "task_id": {"type": "string"},
                    "wait_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict, context: ToolContext) -> ToolResult:
        try:
            self._validate(arguments)
            operation = arguments["operation"]
            if operation == "start":
                return self._start(arguments, context)
            task_id = arguments["task_id"]
            if operation == "status":
                return ToolResult.success("agent", _snapshot_data(self.manager.status(self.scope, task_id)))
            if operation == "wait":
                outcome = self.manager.wait(self.scope, task_id, arguments.get("wait_seconds", 30))
                return ToolResult.success(
                    "agent",
                    {
                        "task": _snapshot_data(outcome.snapshot),
                        "timed_out_wait": outcome.timed_out,
                        "result": _result_data(outcome.result, context),
                    },
                )
            if operation == "background":
                return ToolResult.success("agent", _snapshot_data(self.manager.background(self.scope, task_id)))
            if operation == "cancel":
                return ToolResult.success("agent", _snapshot_data(self.manager.cancel(self.scope, task_id)))
            if operation == "collect":
                return ToolResult.success("agent", _result_data(self.manager.collect(self.scope, task_id), context))
            raise ToolFailure(_INVALID, "子任务请求无效。")
        except SubAgentManagerError as exc:
            code = exc.code if exc.code in _SAFE_MANAGER_CODES else "subagent_provider_error"
            return ToolResult.failure("agent", code, _safe_message(code))
        except ToolFailure as exc:
            return ToolResult.failure("agent", exc.code, exc.message, exc.details)
        except Exception:
            return ToolResult.failure("agent", "subagent_provider_error", "子任务操作未能完成。")

    def _start(self, arguments: dict, context: ToolContext) -> ToolResult:
        # 没有主线程发布的权限/可见性快照时，不创建任何 child task。
        self.manager.policy_snapshot(self.scope)
        kind = arguments["kind"]
        if kind == "definition":
            definition = next(
                (item for item in self.catalog.definitions if item.name == arguments["agent_name"]),
                None,
            )
            if definition is None:
                raise ToolFailure("subagent_not_found", "指定的 Agent 定义不可用。")
            payload = DefinitionTask(definition, mode=self.mode_provider())
        else:
            messages = capture_fork_snapshot(self.snapshot_provider(), context.sensitive_values)
            payload = ForkTask(
                messages=messages,
                mode=self.mode_provider(),
                model=arguments.get("model_override"),
                allowlist=tuple(arguments["allowlist"]) if "allowlist" in arguments else None,
            )
        started = self.manager.start(
            self.scope,
            arguments["task_prompt"],
            execution=TaskExecution(arguments["execution"]),
            payload=payload,
        )
        return ToolResult.success("agent", _snapshot_data(started))

    @staticmethod
    def _validate(arguments: object) -> None:
        if not isinstance(arguments, dict) or set(arguments) - _ALLOWED_FIELDS:
            raise ToolFailure(_INVALID, "子任务请求参数无效。")
        operation = arguments.get("operation")
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            raise ToolFailure(_INVALID, "子任务操作无效。")
        if operation == "start":
            if not {"kind", "task_prompt", "execution"}.issubset(arguments):
                raise ToolFailure(_INVALID, "启动子任务缺少必要参数。")
            if not isinstance(arguments["task_prompt"], str) or not arguments["task_prompt"].strip():
                raise ToolFailure(_INVALID, "任务说明必须是非空字符串。")
            if not isinstance(arguments["execution"], str) or arguments["execution"] not in {item.value for item in TaskExecution}:
                raise ToolFailure(_INVALID, "任务执行模式无效。")
            kind = arguments["kind"]
            if kind == "definition":
                if set(arguments) - {"operation", "kind", "task_prompt", "execution", "agent_name"}:
                    raise ToolFailure(_INVALID, "Definition 启动参数不兼容。")
                if not isinstance(arguments.get("agent_name"), str) or not arguments["agent_name"]:
                    raise ToolFailure(_INVALID, "Definition 任务需要 agent_name。")
            elif kind == "fork":
                if set(arguments) - {"operation", "kind", "task_prompt", "execution", "model_override", "allowlist"}:
                    raise ToolFailure(_INVALID, "Fork 启动参数不兼容。")
                if "agent_name" in arguments:
                    raise ToolFailure(_INVALID, "Fork 任务不接受 agent_name。")
                if "model_override" in arguments and (
                    not isinstance(arguments["model_override"], str)
                    or not arguments["model_override"].strip()
                    or len(arguments["model_override"]) > 128
                ):
                    raise ToolFailure(_INVALID, "Fork model_override 无效。")
                if "allowlist" in arguments and (
                    not isinstance(arguments["allowlist"], list)
                    or any(not isinstance(item, str) or not item for item in arguments["allowlist"])
                    or len(set(arguments["allowlist"])) != len(arguments["allowlist"])
                ):
                    raise ToolFailure(_INVALID, "Fork allowlist 无效。")
            else:
                raise ToolFailure(_INVALID, "任务 kind 无效。")
            return

        if not isinstance(arguments.get("task_id"), str) or not arguments["task_id"] or len(arguments["task_id"]) > 128:
            raise ToolFailure(_INVALID, "该操作需要有效 task_id。")
        allowed = {"operation", "task_id", "wait_seconds"} if operation == "wait" else {"operation", "task_id"}
        if set(arguments) - allowed:
            raise ToolFailure(_INVALID, "该操作包含不支持的参数。")
        if operation == "wait" and "wait_seconds" in arguments:
            value = arguments["wait_seconds"]
            if type(value) is not int or not 1 <= value <= 30:
                raise ToolFailure(_INVALID, "wait_seconds 必须是 1 到 30 的整数。")


def _snapshot_data(snapshot) -> dict:
    return {
        "task_id": snapshot.task_id,
        "state": snapshot.state.value,
        "execution": snapshot.execution.value,
        "rounds": snapshot.rounds,
        "input_tokens": snapshot.input_tokens,
        "output_tokens": snapshot.output_tokens,
        "error_code": snapshot.error_code,
        "result_available": snapshot.result_available,
    }


def _result_data(result, context: ToolContext) -> dict | None:
    if result is None:
        return None
    return {
        "task_id": result.task_id,
        "state": result.state.value,
        "error_code": result.error_code,
        "summary": redact_text(result.summary, context.sensitive_values)[:MAX_TASK_SUMMARY_CHARS],
    }


def _safe_message(code: str) -> str:
    if code == "subagent_not_found":
        return "未找到当前会话中的子任务。"
    if code == "subagent_result_not_ready":
        return "子任务尚未完成。"
    if code == "subagent_result_already_collected":
        return "子任务结果已领取。"
    return "子任务操作未能完成。"
