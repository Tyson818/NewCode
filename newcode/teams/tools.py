"""固定 Team 工具契约；模型参数不能携带运行时身份。"""

from __future__ import annotations

from dataclasses import dataclass
import re

from newcode.tools.types import ToolSpec, ToolResult
from .types import TeamError, validate_id, validate_name


@dataclass(frozen=True)
class Operation:
    roles: tuple[str, ...]
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()


L = ("lead",)
M = ("member",)
B = ("lead", "member")
OPERATIONS = {
    "team_status": {
        "status": Operation(B), "roster": Operation(B, optional=("cursor", "limit")),
        "tasks": Operation(B, optional=("task_id", "cursor", "limit")),
        "inbox": Operation(B, optional=("cursor", "limit")),
        "plan": Operation(B, ("plan_id",)),
    },
    "team_manage": {
        "create": Operation(L, ("team_name",), ("backend",)),
        "spawn": Operation(L, ("member_name", "agent_name", "task_id", "expected_team_revision"),
                           ("backend", "require_plan_approval")),
        "pause": Operation(L, ("target_member_id", "expected_member_revision")),
        "resume": Operation(L, ("target_member_id", "expected_member_revision"), ("task_id",)),
        "stop": Operation(L, ("target_member_id", "expected_member_revision")),
        "approve_plan": Operation(L, ("plan_id", "expected_plan_revision"), ("reason",)),
        "reject_plan": Operation(L, ("plan_id", "expected_plan_revision", "reason")),
        "archive": Operation(L, ("expected_team_revision",)),
    },
    "team_tasks": {
        "create": Operation(L, ("title", "description", "dependencies")),
        "claim": Operation(M, ("task_id", "expected_task_revision")),
        "assign": Operation(L, ("task_id", "target_member_id", "expected_task_revision")),
        "update": Operation(B, ("task_id", "expected_task_revision", "changes")),
        "complete": Operation(B, ("task_id", "expected_task_revision", "summary")),
        "delete": Operation(L, ("task_id", "expected_task_revision")),
        "submit_plan": Operation(M, ("task_id", "content"), ("plan_id", "expected_plan_revision")),
    },
    "team_message": {
        "send": Operation(B, ("recipient", "body", "idempotency_key")),
        "broadcast": Operation(B, ("body", "idempotency_key")),
        "read": Operation(B, optional=("cursor", "limit")),
        "ack": Operation(B, ("message_id",)),
    },
    "team_integrate": {
        "preview": Operation(L, ("target_member_id", "expected_member_revision")),
        "integrate": Operation(L, ("target_member_id", "expected_member_revision", "expected_target_head",
                                  "snapshot_digest", "idempotency_key", "confirmation")),
    },
}
TEAM_TOOLS = frozenset(OPERATIONS)
MEMBER_TOOLS = frozenset({"team_status", "team_tasks", "team_message"})


def _field(name):
    if name.startswith("expected_") and name.endswith("revision"):
        return {"type": "integer", "minimum": 1}
    if name == "limit":
        return {"type": "integer", "minimum": 1, "maximum": 50}
    if name in {"confirmation", "require_plan_approval"}:
        return {"type": "boolean", **({"const": True} if name == "confirmation" else {})}
    if name == "backend":
        return {"type": "string", "enum": ["lightweight", "pane"]}
    if name == "dependencies":
        return {"type": "array", "maxItems": 16, "uniqueItems": True, "items": {"type": "string"}}
    if name == "changes":
        return {"type": "object", "minProperties": 1, "additionalProperties": False,
                "properties": {key: _field(key) for key in ("title", "description", "dependencies", "status")}}
    if name == "status":
        return {"type": "string", "enum": ["in_progress", "blocked", "cancelled"]}
    return {"type": "string", "maxLength": {"body": 8192, "content": 8192,
            "description": 8192, "title": 240, "reason": 2000, "summary": 2000,
            "team_name": 80, "cursor": 2048}.get(name, 128),
            "minLength": 0 if name in {"description", "summary"} else 1}


def tool_schema(name):
    """按 operation 封闭字段集合；角色与状态仍由后端重复校验。"""
    fields = {key for contract in OPERATIONS[name].values() for key in contract.required + contract.optional}
    return {"type": "object", "additionalProperties": False, "required": ["operation"],
        "properties": {"operation": {"type": "string", "enum": list(OPERATIONS[name])},
                       **{key: _field(key) for key in sorted(fields)}},
        "oneOf": [{"type": "object", "additionalProperties": False,
        "properties": {"operation": {"type": "string", "const": op},
                       **{key: _field(key) for key in contract.required + contract.optional}},
        "required": ["operation", *contract.required]}
        for op, contract in OPERATIONS[name].items()]}


def _validate_field(key, value):
    schema = _field(key)
    kind = schema["type"]
    if kind == "integer":
        valid = type(value) is int and schema["minimum"] <= value <= schema.get("maximum", 2**53 - 1)
    elif kind == "boolean":
        valid = type(value) is bool and (key != "confirmation" or value is True)
    elif kind == "array":
        valid = (type(value) is list and len(value) <= 16 and all(type(x) is str for x in value)
                 and len(set(value)) == len(value))
        if valid:
            for item in value:
                validate_id(item, "task")
    elif kind == "object":
        valid = type(value) is dict and bool(value) and not set(value) - set(schema["properties"])
        if valid:
            for k, v in value.items():
                _validate_field(k, v)
    else:
        valid = (type(value) is str and schema.get("minLength", 1) <= len(value) <= schema.get("maxLength", 128)
                 and ("enum" not in schema or value in schema["enum"]))
        if valid and key in {"body", "content", "description"}:
            valid = len(value.encode("utf-8")) <= 8192
    if not valid:
        raise TeamError("team_arguments_invalid")
    if key in {"task_id", "message_id", "plan_id"}:
        validate_id(value, key[:-3])
    if key in {"target_member_id", "member_name", "agent_name", "recipient"}:
        if validate_name(value) != value:
            raise TeamError("team_arguments_invalid")
    patterns = {"idempotency_key": r"[A-Za-z0-9_-]{1,64}",
                "expected_target_head": r"(?:[0-9a-f]{40}|[0-9a-f]{64})", "snapshot_digest": r"[0-9a-f]{64}"}
    if key in patterns and not re.fullmatch(patterns[key], value):
        raise TeamError("team_arguments_invalid")


def validate_arguments(name, arguments, role):
    if type(arguments) is not dict or type(arguments.get("operation")) is not str:
        raise TeamError("team_arguments_invalid")
    op = arguments["operation"]
    contract = OPERATIONS[name].get(op)
    if contract is None or set(arguments) - {"operation", *contract.required, *contract.optional}:
        raise TeamError("team_arguments_invalid")
    if not set(contract.required) <= set(arguments):
        raise TeamError("team_arguments_invalid")
    if role not in contract.roles:
        raise TeamError("team_role_denied")
    for key, value in arguments.items():
        if key != "operation":
            _validate_field(key, value)
    if name == "team_status" and op == "tasks" and "task_id" in arguments and {"cursor", "limit"} & arguments.keys():
        raise TeamError("team_arguments_invalid")
    if op == "submit_plan" and (("plan_id" in arguments) != ("expected_plan_revision" in arguments)):
        raise TeamError("team_arguments_invalid")
    if op == "update" and role == "member" and (
        set(arguments["changes"]) != {"status"} or arguments["changes"]["status"] == "cancelled"
    ):
        raise TeamError("team_role_denied")
    return op


class TeamTool:
    """身份和 Permission admission 均来自可信 runtime，不从参数读取。"""
    def __init__(self, name, runtime):
        if name not in TEAM_TOOLS:
            raise ValueError("team_arguments_invalid")
        self.runtime = runtime
        self.spec = ToolSpec(name, "受控团队操作；身份由运行时绑定。", tool_schema(name))
        self.execution_roles = B if name in MEMBER_TOOLS else L

    def run(self, arguments, context):
        try:
            validate_arguments(self.spec.name, arguments, self.runtime.role)
            return ToolResult.success(self.spec.name, self.runtime.invoke(self.spec.name, arguments, context))
        except TeamError as exc:
            return ToolResult.failure(self.spec.name, exc.code, "团队操作未完成。")
        except Exception:
            return ToolResult.failure(self.spec.name, "team_worker_failed", "团队操作未完成。")


def register_team_tools(registry, runtime):
    registry.execution_role = runtime.role
    names = TEAM_TOOLS if runtime.role == "lead" else MEMBER_TOOLS if runtime.role == "member" else ()
    for name in sorted(names):
        registry.register(TeamTool(name, runtime), read_only=name == "team_status", do_visible=True)


def registry_without_team_tools(registry):
    from newcode.tools.registry import ToolRegistry
    result = ToolRegistry()
    for name in registry.names():
        if name not in TEAM_TOOLS and name != "load_skill":
            result.register(registry.get(name), read_only=registry.is_read_only(name),
                            do_visible=name in registry.do_visible_names())
    return result
