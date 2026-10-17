"""Team 纯数据、不可变策略上限与有限状态转换。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
import json
import re
import secrets
import unicodedata
from types import MappingProxyType
from typing import Any

SCHEMA_VERSION = 1
MAX_TEAMS = 32
MAX_MEMBERS = 8
MAX_RUNNING_MEMBERS = 4
MAX_QUEUED_MEMBERS = 16
MAX_TASKS = 256
MAX_DEPENDENCIES = 16
MAX_MESSAGES = 4096
MAX_PENDING_MESSAGES = 1024
MAX_MESSAGE_BYTES = 16 * 1024
MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
MAX_TRANSACTION_BYTES = 512 * 1024
MAX_TRANSACTIONS = 8192
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_MEMBER_ROUNDS = 8
MAX_MEMBER_SECONDS = 300
MAX_MEMBER_TOKENS = 16_000
MAX_WAIT_SECONDS = 30
SHUTDOWN_SECONDS = 1.0

ERROR_CODES = frozenset("""
team_config_invalid team_path_invalid team_store_unavailable team_store_corrupt
team_store_busy team_workspace_mismatch team_limit_exceeded team_member_not_found
team_member_busy team_backend_unavailable team_backend_start_failed team_worker_failed
team_shutdown_timeout team_task_conflict team_task_dependency_invalid
team_task_dependency_cycle team_task_not_ready team_mailbox_full team_message_invalid
team_approval_required team_plan_stale team_arguments_invalid team_role_denied
team_scope_invalid disallowed_tool team_policy_changed permission_denied
team_state_invalid team_revision_conflict team_cursor_invalid team_task_not_found
team_plan_not_found team_plan_access_denied team_name_conflict team_definition_invalid
team_restore_invalid team_integration_pending team_task_delete_denied
team_recipient_invalid team_idempotency_conflict team_message_not_found
team_message_not_owned team_ack_checkpoint_required team_worktree_owner_invalid
team_target_dirty team_snapshot_invalid team_target_changed team_single_writer_required
team_integration_conflict team_integration_outcome_unknown team_integration_failed
team_coordinator_unavailable
""".split())


class TeamError(ValueError):
    """不保留原始异常、路径或调用参数。"""

    def __init__(self, code: str) -> None:
        self.code = code if code in ERROR_CODES else "team_arguments_invalid"
        super().__init__(self.code)


class TeamRole(str, Enum):
    LEAD = "lead"
    MEMBER = "member"


class TeamState(str, Enum):
    READY = "ready"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


class MemberState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    IDLE = "idle"
    RESUMING = "resuming"
    STOPPING = "stopping"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    INTEGRATED = "integrated"


class TaskState(str, Enum):
    OPEN = "open"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PlanState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class Backend(str, Enum):
    LIGHTWEIGHT = "lightweight"
    PANE = "pane"


class MessageKind(str, Enum):
    MESSAGE = "team.message.v1"
    TASK_ASSIGNED = "team.task.assigned.v1"
    TASK_COMPLETED = "team.task.completed.v1"
    PLAN_SUBMITTED = "team.plan.submitted.v1"
    PLAN_DECISION = "team.plan.decision.v1"
    MEMBER_IDLE = "team.member.idle.v1"
    MEMBER_FAILED = "team.member.failed.v1"


_TEAM_TRANSITIONS = {
    "ready": {"active", "closing"}, "active": {"closing"},
    "closing": {"ready", "closed"}, "closed": {"ready"},
}
_MEMBER_TRANSITIONS = {
    "created": {"starting", "stopping"},
    "starting": {"awaiting_approval", "running", "idle", "stopping", "failed", "interrupted"},
    "awaiting_approval": {"running", "idle", "pausing", "stopping", "failed", "interrupted"},
    "running": {"awaiting_approval", "idle", "pausing", "stopping", "failed", "interrupted"},
    "pausing": {"paused", "stopping", "failed", "interrupted"},
    "paused": {"resuming", "stopping"},
    "idle": {"resuming", "stopping", "integrated"},
    "resuming": {"running", "awaiting_approval", "stopping", "failed", "interrupted"},
    "stopping": {"idle", "interrupted", "failed"},
    "interrupted": {"starting", "stopping"}, "failed": {"starting", "stopping"},
    "integrated": set(),
}
_TASK_TRANSITIONS = {
    "open": {"assigned", "cancelled"}, "assigned": {"in_progress", "blocked", "cancelled"},
    "in_progress": {"blocked", "completed", "cancelled"},
    "blocked": {"assigned", "in_progress", "cancelled"}, "completed": set(), "cancelled": set(),
}
_PLAN_TRANSITIONS = {"pending": {"approved", "rejected", "superseded"},
                     "approved": {"superseded"}, "rejected": {"superseded"}, "superseded": set()}


def validate_transition(current: Enum, target: Enum) -> None:
    maps = {TeamState: _TEAM_TRANSITIONS, MemberState: _MEMBER_TRANSITIONS,
            TaskState: _TASK_TRANSITIONS, PlanState: _PLAN_TRANSITIONS}
    if type(current) is not type(target) or type(current) not in maps:
        raise TeamError("team_state_invalid")
    if target.value not in maps[type(current)].get(current.value, set()):
        raise TeamError("team_state_invalid")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(kind: str) -> str:
    if kind not in {"team", "task", "message", "plan", "transaction", "mutation", "broadcast", "integration"}:
        raise TeamError("team_arguments_invalid")
    return f"{kind}-{secrets.token_hex(16)}"


def validate_id(value: object, kind: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"{kind}-[0-9a-f]{{32}}", value):
        raise TeamError("team_path_invalid")
    return value


def validate_name(value: object) -> str:
    if not isinstance(value, str):
        raise TeamError("team_path_invalid")
    result = value.casefold()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", result) or result in {
        "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))
    }:
        raise TeamError("team_path_invalid")
    return result


def text(value: object, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise TeamError("team_arguments_invalid")
    if any(ord(c) < 32 and c not in "\n\t" for c in value) or len(value) > maximum:
        raise TeamError("team_arguments_invalid")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise TeamError("team_arguments_invalid") from None
    return value


def positive(value: object, *, zero: bool = False) -> int:
    if type(value) is not int or value < (0 if zero else 1):
        raise TeamError("team_arguments_invalid")
    return value


def _enum(record: object, key: str, cls: type[Enum]) -> None:
    try:
        object.__setattr__(record, key, cls(getattr(record, key)))
    except (ValueError, TypeError):
        raise TeamError("team_arguments_invalid") from None


def _timestamp(value: object) -> None:
    try:
        if not isinstance(value, str) or datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError
    except ValueError:
        raise TeamError("team_arguments_invalid") from None


def freeze(value: Any, depth: int = 0) -> Any:
    """拒绝任意运行对象及非有限数值，而不是调用其 str/repr。"""
    if depth > 24:
        raise TeamError("team_arguments_invalid")
    if isinstance(value, Enum):
        return value.value
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        try:
            json.dumps(value, allow_nan=False)
        except ValueError:
            raise TeamError("team_arguments_invalid") from None
        return value
    if isinstance(value, Mapping):
        if any(type(k) is not str for k in value):
            raise TeamError("team_arguments_invalid")
        return MappingProxyType({k: freeze(v, depth + 1) for k, v in value.items()})
    if isinstance(value, (list, tuple, frozenset)):
        return tuple(freeze(v, depth + 1) for v in value)
    raise TeamError("team_arguments_invalid")


def thaw(value: Any) -> Any:
    if isinstance(value, Record):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, frozenset)):
        return [thaw(v) for v in sorted(value)] if isinstance(value, frozenset) else [thaw(v) for v in value]
    return value


class Record:
    def to_dict(self) -> dict[str, Any]:
        return {f.name: thaw(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_dict(cls, value: object):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise TeamError("team_arguments_invalid")
        try:
            return cls(**value)
        except (TypeError, KeyError):
            raise TeamError("team_arguments_invalid") from None


@dataclass(frozen=True)
class TeamMemberRecord(Record):
    member_id: str
    name: str
    role: TeamRole = TeamRole.MEMBER
    agent_name: str = ""
    state: MemberState = MemberState.CREATED
    backend: Backend = Backend.LIGHTWEIGHT
    requires_plan_approval: bool = True
    auto_wake: bool = False
    revision: int = 1
    worktree_relative: str | None = None
    base_commit: str | None = None
    checkpoint_session_id: str | None = None
    plan_revision: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    runtime_generation: int = 0

    def __post_init__(self) -> None:
        if validate_name(self.member_id) != self.member_id:
            raise TeamError("team_arguments_invalid")
        object.__setattr__(self, "name", validate_name(self.name))
        _enum(self, "role", TeamRole)
        _enum(self, "state", MemberState)
        _enum(self, "backend", Backend)
        text(self.agent_name, 64, empty=self.role is TeamRole.LEAD)
        if self.role is TeamRole.MEMBER and not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", self.agent_name):
            raise TeamError("team_arguments_invalid")
        if type(self.requires_plan_approval) is not bool or type(self.auto_wake) is not bool:
            raise TeamError("team_arguments_invalid")
        positive(self.revision)
        for field in ("plan_revision", "input_tokens", "output_tokens", "runtime_generation"):
            positive(getattr(self, field), zero=True)
        if self.base_commit is not None and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.base_commit):
            raise TeamError("team_arguments_invalid")
        if self.worktree_relative is not None:
            if not re.fullmatch(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+", self.worktree_relative):
                raise TeamError("team_path_invalid")
        if self.checkpoint_session_id is not None and not re.fullmatch(r"\d{8}-\d{6}-[a-z0-9]{4}", self.checkpoint_session_id):
            raise TeamError("team_arguments_invalid")


@dataclass(frozen=True)
class TeamManifest(Record):
    team_id: str
    name: str
    workspace_fingerprint: str
    lead_member_id: str
    roster: tuple[TeamMemberRecord, ...]
    created_at: str
    updated_at: str
    revision: int = 1
    state: TeamState = TeamState.READY
    backend: Backend = Backend.LIGHTWEIGHT
    version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_id(self.team_id, "team")
        text(self.name, 80)
        if any(unicodedata.category(c).startswith("C") for c in self.name):
            raise TeamError("team_arguments_invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.workspace_fingerprint):
            raise TeamError("team_arguments_invalid")
        validate_name(self.lead_member_id)
        if not isinstance(self.roster, (tuple, list)) or not 1 <= len(self.roster) <= MAX_MEMBERS + 1:
            raise TeamError("team_limit_exceeded")
        members = tuple(m if isinstance(m, TeamMemberRecord) else TeamMemberRecord.from_dict(m) for m in self.roster)
        if len({m.member_id.casefold() for m in members}) != len(members) or len({m.name.casefold() for m in members}) != len(members):
            raise TeamError("team_name_conflict")
        leads = [m for m in members if m.role is TeamRole.LEAD]
        if len(leads) != 1 or leads[0].member_id != self.lead_member_id:
            raise TeamError("team_arguments_invalid")
        object.__setattr__(self, "roster", members)
        _enum(self, "state", TeamState)
        _enum(self, "backend", Backend)
        positive(self.revision)
        if type(self.version) is not int or self.version != SCHEMA_VERSION:
            raise TeamError("team_arguments_invalid")
        _timestamp(self.created_at)
        _timestamp(self.updated_at)


@dataclass(frozen=True)
class TeamTask(Record):
    task_id: str
    title: str
    description: str
    creator: str
    created_at: str
    updated_at: str
    dependencies: tuple[str, ...] = ()
    assignee: str | None = None
    state: TaskState = TaskState.OPEN
    revision: int = 1
    summary: str = ""
    deleted: bool = False

    def __post_init__(self) -> None:
        validate_id(self.task_id, "task")
        text(self.title, 240)
        text(self.description, 8192, empty=True)
        if len(self.description.encode("utf-8")) > 8192:
            raise TeamError("team_limit_exceeded")
        validate_name(self.creator)
        if self.assignee is not None:
            validate_name(self.assignee)
        if not isinstance(self.dependencies, (list, tuple)) or len(self.dependencies) > MAX_DEPENDENCIES:
            raise TeamError("team_task_dependency_invalid")
        deps = tuple(validate_id(d, "task") for d in self.dependencies)
        if len(set(deps)) != len(deps) or self.task_id in deps:
            raise TeamError("team_task_dependency_invalid")
        object.__setattr__(self, "dependencies", deps)
        _enum(self, "state", TaskState)
        positive(self.revision)
        text(self.summary, 2000, empty=True)
        if type(self.deleted) is not bool:
            raise TeamError("team_arguments_invalid")
        _timestamp(self.created_at)
        _timestamp(self.updated_at)


@dataclass(frozen=True)
class TeamMessage(Record):
    message_id: str
    team_id: str
    sender_member_id: str
    recipient: str
    body: str
    created_at: str
    sequence: int
    idempotency_key: str
    kind: MessageKind = MessageKind.MESSAGE
    summary: str = ""
    broadcast_id: str | None = None
    payload: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        validate_id(self.message_id, "message")
        validate_id(self.team_id, "team")
        validate_name(self.sender_member_id)
        validate_name(self.recipient)
        text(self.body, MAX_MESSAGE_BYTES)
        if len(self.body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise TeamError("team_limit_exceeded")
        positive(self.sequence)
        if not isinstance(self.idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.idempotency_key):
            raise TeamError("team_arguments_invalid")
        _timestamp(self.created_at)
        _enum(self, "kind", MessageKind)
        text(self.summary, 2000, empty=True)
        if self.broadcast_id is not None:
            validate_id(self.broadcast_id, "broadcast")
        if self.payload is not None:
            if not isinstance(self.payload, Mapping):
                raise TeamError("team_arguments_invalid")
            object.__setattr__(self, "payload", freeze(self.payload))


@dataclass(frozen=True)
class TeamPlan(Record):
    plan_id: str
    member_id: str
    task_id: str
    content: str
    submitted_at: str
    revision: int = 1
    state: PlanState = PlanState.PENDING
    decision_member_id: str | None = None
    decision_at: str | None = None
    message_id: str | None = None

    def __post_init__(self) -> None:
        validate_id(self.plan_id, "plan")
        validate_id(self.task_id, "task")
        validate_name(self.member_id)
        text(self.content, 8192)
        if len(self.content.encode("utf-8")) > 8192:
            raise TeamError("team_limit_exceeded")
        positive(self.revision)
        _enum(self, "state", PlanState)
        _timestamp(self.submitted_at)
        if self.decision_member_id is not None:
            validate_name(self.decision_member_id)
        if self.decision_at is not None:
            _timestamp(self.decision_at)
        if self.state in (PlanState.APPROVED, PlanState.REJECTED) and (self.decision_member_id is None or self.decision_at is None):
            raise TeamError("team_arguments_invalid")
        if self.state is PlanState.PENDING and (self.decision_member_id is not None or self.decision_at is not None):
            raise TeamError("team_arguments_invalid")
        if self.message_id is not None:
            validate_id(self.message_id, "message")


@dataclass(frozen=True)
class TeamAck(Record):
    """每个收件人的已确认 envelope ID 投影，由事务日志重建。"""

    member_id: str
    acknowledged_message_ids: tuple[str, ...] = ()
    revision: int = 1

    def __post_init__(self) -> None:
        if validate_name(self.member_id) != self.member_id:
            raise TeamError("team_scope_invalid")
        positive(self.revision)
        if not isinstance(self.acknowledged_message_ids, (tuple, list)) or len(self.acknowledged_message_ids) > MAX_MESSAGES:
            raise TeamError("team_limit_exceeded")
        values = tuple(validate_id(value, "message") for value in self.acknowledged_message_ids)
        if len(set(values)) != len(values):
            raise TeamError("team_arguments_invalid")
        object.__setattr__(self, "acknowledged_message_ids", values)


@dataclass(frozen=True)
class TeamScope(Record):
    team_id: str
    member_id: str
    session_id: str
    generation: int

    def __post_init__(self) -> None:
        validate_id(self.team_id, "team")
        if validate_name(self.member_id) != self.member_id:
            raise TeamError("team_scope_invalid")
        if not isinstance(self.session_id, str) or not re.fullmatch(r"\d{8}-\d{6}-[a-z0-9]{4}", self.session_id):
            raise TeamError("team_scope_invalid")
        positive(self.generation)


@dataclass(frozen=True)
class TeamPolicySnapshot(Record):
    """只保存上限与版本，不保存 Permission grant/session allow。"""

    scope: TeamScope
    revision: int
    startup_tools: frozenset[str]
    current_tools: frozenset[str]
    plan_state: PlanState = PlanState.PENDING
    plan_revision: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TeamScope):
            object.__setattr__(self, "scope", TeamScope.from_dict(self.scope))
        positive(self.revision)
        positive(self.plan_revision, zero=True)
        for field in ("startup_tools", "current_tools"):
            raw = getattr(self, field)
            if not isinstance(raw, (list, tuple, set, frozenset)) or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for v in raw):
                raise TeamError("team_arguments_invalid")
            object.__setattr__(self, field, frozenset(raw))
        if not self.current_tools <= self.startup_tools:
            raise TeamError("team_policy_changed")
        _enum(self, "plan_state", PlanState)


def validate_policy_update(previous: TeamPolicySnapshot, updated: TeamPolicySnapshot) -> None:
    """审批门可变化，但绝不能借此恢复 Pt 已移除的能力。"""
    if previous.scope != updated.scope or previous.startup_tools != updated.startup_tools or updated.revision <= previous.revision or not updated.current_tools <= previous.current_tools:
        raise TeamError("team_policy_changed")


@dataclass(frozen=True)
class DeliveryProof(Record):
    team_id: str
    member_id: str
    session_id: str
    generation: int
    processed_message_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_id(self.team_id, "team")
        validate_name(self.member_id)
        if not re.fullmatch(r"\d{8}-\d{6}-[a-z0-9]{4}", self.session_id):
            raise TeamError("team_arguments_invalid")
        positive(self.generation)
        if not isinstance(self.processed_message_ids, (list, tuple)) or len(self.processed_message_ids) > MAX_MESSAGES:
            raise TeamError("team_limit_exceeded")
        ids = tuple(validate_id(v, "message") for v in self.processed_message_ids)
        if len(set(ids)) != len(ids):
            raise TeamError("team_arguments_invalid")
        object.__setattr__(self, "processed_message_ids", ids)


INTEGRATION_TRANSITIONS = {
    "frozen": {"source_preparing", "outcome_unknown"},
    "source_preparing": {"source_preparing", "source_committed", "outcome_unknown"},
    "source_committed": {"merge_preparing", "outcome_unknown"},
    "merge_preparing": {"merge_preparing", "merged", "conflict", "outcome_unknown"},
    "merged": {"applying_target", "outcome_unknown"},
    "applying_target": {"completed", "safe_not_applied", "outcome_unknown"},
    "safe_not_applied": {"applying_target", "outcome_unknown"},
    "outcome_unknown": set(), "conflict": set(), "completed": set(),
}


@dataclass(frozen=True)
class IntegrationRecord(Record):
    """集成提交日志的版本化 after-image；不保存源码、凭据或异常。"""
    integration_id: str
    team_id: str
    member_id: str
    member_revision: int
    generation: int
    base_commit: str
    target_head: str
    target_branch: str
    snapshot_digest: str
    idempotency_key: str
    target_signature: str
    manifest: tuple[Mapping[str, Any], ...]
    stage: str = "frozen"
    source_tree: str | None = None
    source_commit: str | None = None
    merge_tree: str | None = None
    integrated_commit: str | None = None
    revision: int = 1
    version: int = 1

    def __post_init__(self):
        validate_id(self.integration_id, "integration")
        validate_id(self.team_id, "team")
        if validate_name(self.member_id) != self.member_id or self.member_id == "lead":
            raise TeamError("team_scope_invalid")
        for value in (self.member_revision, self.generation, self.revision):
            positive(value)
        if (type(self.version) is not int or self.version != 1
                or type(self.stage) is not str or self.stage not in INTEGRATION_TRANSITIONS):
            raise TeamError("team_arguments_invalid")
        for value in (self.base_commit, self.target_head, self.source_tree, self.source_commit, self.merge_tree, self.integrated_commit):
            if value is not None and (type(value) is not str or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value)):
                raise TeamError("team_arguments_invalid")
        if self.base_commit is None or self.target_head is None:
            raise TeamError("team_arguments_invalid")
        for value in (self.snapshot_digest, self.target_signature):
            if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise TeamError("team_arguments_invalid")
        branch = self.target_branch
        if (type(branch) is not str or not branch or len(branch) > 240 or branch.startswith(("-", "/", "."))
                or any(c.isspace() or ord(c) < 32 or c in "~^:?*[\\" for c in branch)
                or ".." in branch or "@{" in branch or any(not p or p.startswith(".") or p.endswith((".", ".lock")) for p in branch.split("/"))):
            raise TeamError("team_arguments_invalid")
        if type(self.idempotency_key) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.idempotency_key):
            raise TeamError("team_arguments_invalid")
        if not isinstance(self.manifest, (tuple, list)) or len(self.manifest) > 4096:
            raise TeamError("team_limit_exceeded")
        names, total = set(), 0
        keys = {"path", "base_object_id", "staged_object_id", "mode", "staged_mode", "sha256", "binary", "size"}
        for item in self.manifest:
            if not isinstance(item, Mapping) or set(item) != keys:
                raise TeamError("team_arguments_invalid")
            path = item["path"]
            if (type(path) is not str or len(path) > 1024 or path.startswith(("/", "-", ":"))
                    or any(c in path for c in "\\:\0\r\n")
                    or any(p in {"", ".", "..", ".git"} or p.endswith((".", " ")) for p in path.split("/"))
                    or path.casefold() in names):
                raise TeamError("team_path_invalid")
            names.add(path.casefold())
            if type(item["size"]) is not int or not 0 <= item["size"] <= 1024 * 1024 or type(item["binary"]) is not bool:
                raise TeamError("team_arguments_invalid")
            total += item["size"]
            for key in ("mode", "staged_mode"):
                if item[key] not in {None, "100644", "100755"}:
                    raise TeamError("team_arguments_invalid")
            for key, pattern in (("sha256", r"[0-9a-f]{64}"), ("base_object_id", r"(?:[0-9a-f]{40}|[0-9a-f]{64})"),
                                 ("staged_object_id", r"(?:[0-9a-f]{40}|[0-9a-f]{64})")):
                if item[key] is not None and (type(item[key]) is not str or not re.fullmatch(pattern, item[key])):
                    raise TeamError("team_arguments_invalid")
            if (item["mode"] is None) != (item["sha256"] is None) or item["mode"] is None and item["size"]:
                raise TeamError("team_arguments_invalid")
        if total > 16 * 1024 * 1024:
            raise TeamError("team_limit_exceeded")
        required = {"source_preparing": (self.source_tree,), "source_committed": (self.source_tree, self.source_commit),
            "merge_preparing": (self.source_tree, self.source_commit),
            "merged": (self.source_tree, self.source_commit, self.merge_tree, self.integrated_commit),
            "applying_target": (self.source_tree, self.source_commit, self.merge_tree, self.integrated_commit),
            "safe_not_applied": (self.source_tree, self.source_commit, self.merge_tree, self.integrated_commit),
            "completed": (self.source_tree, self.source_commit, self.merge_tree, self.integrated_commit)}
        if any(value is None for value in required.get(self.stage, ())):
            raise TeamError("team_arguments_invalid")
        forbidden = {
            "frozen": (self.source_tree, self.source_commit, self.merge_tree, self.integrated_commit),
            "source_preparing": (self.source_commit, self.merge_tree, self.integrated_commit),
            "source_committed": (self.merge_tree, self.integrated_commit),
            "merge_preparing": (self.integrated_commit,),
            "conflict": (self.merge_tree, self.integrated_commit),
        }
        if any(value is not None for value in forbidden.get(self.stage, ())):
            raise TeamError("team_arguments_invalid")
        if self.stage == "conflict" and (self.source_tree is None or self.source_commit is None):
            raise TeamError("team_arguments_invalid")
        object.__setattr__(self, "manifest", freeze(self.manifest))

    def validate_successor(self, other):
        mutable = {"stage", "source_tree", "source_commit", "merge_tree", "integrated_commit", "revision"}
        if (other.revision != self.revision + 1 or other.stage not in INTEGRATION_TRANSITIONS[self.stage]
                or any(getattr(self, f.name) != getattr(other, f.name) for f in fields(self) if f.name not in mutable)
                or any(getattr(self, key) is not None and getattr(self, key) != getattr(other, key)
                       for key in mutable - {"stage", "revision"})):
            raise TeamError("team_state_invalid")
        if other.stage == "outcome_unknown" and any(
                getattr(self, key) != getattr(other, key)
                for key in ("source_tree", "source_commit", "merge_tree", "integrated_commit")):
            raise TeamError("team_state_invalid")


@dataclass(frozen=True)
class RecordChange(Record):
    """内部存储端口的 after-image；没有路径或任意文件名参数。"""

    collection: str
    record_id: str
    value: Mapping[str, Any]

    def __post_init__(self) -> None:
        classes = {"team": TeamManifest, "members": TeamMemberRecord, "tasks": TeamTask,
                   "messages": TeamMessage, "plans": TeamPlan, "acks": TeamAck, "integrations": IntegrationRecord}
        cls = classes.get(self.collection)
        if cls is None or not isinstance(self.value, Mapping):
            raise TeamError("team_arguments_invalid")
        record = cls.from_dict(dict(self.value))
        id_key = {"team": "team_id", "members": "member_id", "tasks": "task_id", "messages": "message_id", "plans": "plan_id", "acks": "member_id", "integrations": "integration_id"}[self.collection]
        if getattr(record, id_key) != self.record_id:
            raise TeamError("team_arguments_invalid")
        object.__setattr__(self, "value", freeze(record.to_dict()))


@dataclass(frozen=True)
class TeamTransaction(Record):
    transaction_id: str
    team_id: str
    commit_sequence: int
    previous_commit_id: str | None
    expected_revision: int
    revision: int
    mutation_id: str
    request_digest: str
    changes: tuple[RecordChange, ...]
    message_high_watermark: int = 0
    idempotency_key: str | None = None
    version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_id(self.transaction_id, "transaction")
        validate_id(self.team_id, "team")
        validate_id(self.mutation_id, "mutation")
        if self.previous_commit_id is not None:
            validate_id(self.previous_commit_id, "transaction")
        positive(self.commit_sequence)
        positive(self.expected_revision, zero=True)
        if type(self.revision) is not int or self.revision != self.expected_revision + 1:
            raise TeamError("team_revision_conflict")
        positive(self.message_high_watermark, zero=True)
        if not isinstance(self.request_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.request_digest):
            raise TeamError("team_arguments_invalid")
        if type(self.version) is not int or self.version != SCHEMA_VERSION:
            raise TeamError("team_arguments_invalid")
        if self.idempotency_key is not None and (not isinstance(self.idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,256}", self.idempotency_key)):
            raise TeamError("team_arguments_invalid")
        if not isinstance(self.changes, (list, tuple)) or not self.changes:
            raise TeamError("team_arguments_invalid")
        changes = tuple(c if isinstance(c, RecordChange) else RecordChange.from_dict(c) for c in self.changes)
        if len({(c.collection, c.record_id) for c in changes}) != len(changes):
            raise TeamError("team_arguments_invalid")
        object.__setattr__(self, "changes", changes)
