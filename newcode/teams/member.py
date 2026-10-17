"""成员 checkpoint 与独立 AgentLoop 运行时；不接入主 CLI。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import secrets
import threading
import time

from newcode.context.estimator import estimate_messages
from newcode.persistence import (
    SESSION_FORMAT_VERSION, STALE_SESSION_AGE, TIME_SPAN_REMINDER_AGE,
    _message_record, _parse_message_record, _parse_metadata,
    _parse_time, _truncate_incomplete_tool_exchange, is_valid_session_id,
    workspace_fingerprint,
)
from newcode.session import ChatMessage, ChatSession

from .store import TeamStore, encode, sanitize
from .types import DeliveryProof, MAX_CHECKPOINT_BYTES, TeamError, freeze, validate_name


@dataclass(frozen=True)
class CheckpointRestore:
    session: ChatSession | None
    proof: DeliveryProof | None = None
    errors: tuple[str, ...] = ()
    truncated: bool = False
    estimated_tokens: int = 0
    needs_compaction: bool = False
    needs_time_span_reminder: bool = False


def delivery_message(envelope: dict) -> ChatMessage:
    """可信投递服务创建的固定 envelope，不把模型文本当成授权。"""
    return ChatMessage("user", encode({"protocol": "team.delivery.v1", "envelope": envelope}).decode("utf-8"))


class MemberCheckpoints:
    def __init__(self, store: TeamStore, team_id: str, member_id: str, *, generation: int) -> None:
        self.store, self.team_id = store, team_id
        self.member_id = validate_name(member_id)
        if self.member_id != member_id or type(generation) is not int or generation < 1:
            raise TeamError("team_scope_invalid")
        self.generation = generation

    def _scope(self):
        view, _, _ = self.store._recover_locked(self.team_id)
        if self.member_id not in view.get("members", {}):
            raise TeamError("team_scope_invalid")
        return view

    def _parts(self, session_id: str) -> tuple[str, ...]:
        if not is_valid_session_id(session_id):
            raise TeamError("team_restore_invalid")
        return (self.team_id, "members", self.member_id, "sessions", f"{session_id}.jsonl")

    def checkpoint(self, session: ChatSession, *, processed_message_ids: tuple[str, ...] = (),
                   now: datetime | None = None) -> DeliveryProof:
        """完整 JSONL 和去重元数据同次发布；失败不改变旧 checkpoint。"""
        if not isinstance(session, ChatSession):
            raise TeamError("team_arguments_invalid")
        current = _utc(now)
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view = self._scope()
            records = []
            for message in tuple(session.messages):
                # 旧归档编码器会 stringify 任意参数对象；此处先拒绝运行对象。
                if message.tool_calls:
                    for call in message.tool_calls:
                        freeze(call.arguments)
                record = sanitize(_message_record(message, self.store.sensitive_values), self.store.sensitive_values)
                for call in record["message"].get("tool_calls", ()):
                    call["raw_arguments"] = encode(call["arguments"]).decode("utf-8")
                records.append(record)
            messages = [_parse_message_record(encode(record).decode("utf-8")) for record in records]
            if any(message is None for message in messages):
                raise TeamError("team_restore_invalid")
            retained, _ = _truncate_incomplete_tool_exchange(messages)
            records = records[:len(retained)]
            session_id = session.session_id
            old_proof = None
            if session_id is not None:
                parts = self._parts(session_id)
                old_path = self.store.paths.checked(*parts, missing=True)
                if old_path.exists():
                    previous = self._restore_locked(session_id, current)
                    if previous.session is None:
                        raise TeamError("team_restore_invalid")
                    old_proof = previous.proof
            else:
                # 与 Chapter9 相同的安全随机四位后缀与16次排他创建上限。
                for _ in range(16):
                    suffix = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
                    candidate = f"{current:%Y%m%d-%H%M%S}-{suffix}"
                    if not self.store.paths.checked(*self._parts(candidate), missing=True).exists():
                        session_id = candidate
                        break
                else:
                    raise TeamError("team_store_unavailable")
            prior_ids = set(old_proof.processed_message_ids) if old_proof else set()
            requested = DeliveryProof(self.team_id, self.member_id, session_id, self.generation, processed_message_ids)
            proof_ids = prior_ids | set(requested.processed_message_ids)
            known = view.get("messages", {})
            for message_id in proof_ids:
                envelope = known.get(message_id)
                if envelope is None or envelope["recipient"] != self.member_id:
                    raise TeamError("team_message_not_owned")
                if message_id not in prior_ids and not _has_delivery(retained, envelope):
                    raise TeamError("team_ack_checkpoint_required")
            proof = DeliveryProof(self.team_id, self.member_id, session_id, self.generation, tuple(sorted(proof_ids)))
            timestamp = current.isoformat()
            body = b"".join(encode(record) + b"\n" for record in records)
            metadata = {
                "record_type": "metadata", "version": SESSION_FORMAT_VERSION,
                "session_id": session_id, "workspace_fingerprint": workspace_fingerprint(self.store.paths.workspace_root),
                "created_at": timestamp, "updated_at": timestamp,
                "team_checkpoint": {"version": 1, "workspace_fingerprint": self.store.paths.workspace_fingerprint,
                                    "proof": proof.to_dict(), "message_digest": hashlib.sha256(body).hexdigest(),
                                    "proof_digest": hashlib.sha256(encode(proof.to_dict()) + body).hexdigest()},
            }
            data = encode(metadata) + b"\n" + body
            if len(data) > MAX_CHECKPOINT_BYTES:
                raise TeamError("team_limit_exceeded")
            parts = self._parts(session_id)
            self.store.paths.mkdir(*parts[:-1])
            exists = self.store.paths.checked(*parts, missing=True).exists()
            self.store._fault("before_checkpoint")
            self.store.atomic_write(parts, data, replace=exists)
            self.store._fault("after_checkpoint")
            session.session_id = session_id
            return proof

    def restore(self, session_id: str, *, now: datetime | None = None) -> CheckpointRestore:
        try:
            with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
                self._scope()
                return self._restore_locked(session_id, _utc(now))
        except (TeamError, OSError, UnicodeError, ValueError, TypeError, RecursionError):
            return CheckpointRestore(None, errors=("team_restore_invalid",))

    def proof_for_ack(self, session_id: str, message_id: str, *, now: datetime | None = None) -> DeliveryProof:
        """只验证交付证明，不实现 Phase2 的 ack 或邮箱服务。"""
        restored = self.restore(session_id, now=now)
        if restored.proof is None or restored.proof.generation != self.generation or message_id not in restored.proof.processed_message_ids:
            raise TeamError("team_ack_checkpoint_required")
        return restored.proof

    def _restore_locked(self, session_id: str, now: datetime) -> CheckpointRestore:
        data = self.store.read_bytes(self._parts(session_id), maximum=MAX_CHECKPOINT_BYTES)
        lines = data.splitlines(keepends=True)
        if not lines:
            raise TeamError("team_restore_invalid")
        metadata = _parse_metadata(lines[0].decode("utf-8"), session_id, self.store.paths.workspace_root)
        if metadata is None:
            raise TeamError("team_restore_invalid")
        updated = _parse_time(metadata.get("updated_at"))
        if now - updated > STALE_SESSION_AGE or updated > now:
            raise TeamError("team_restore_invalid")
        extension = metadata.get("team_checkpoint")
        if not isinstance(extension, dict) or set(extension) != {"version", "workspace_fingerprint", "proof", "message_digest", "proof_digest"} or type(extension["version"]) is not int or extension["version"] != 1:
            raise TeamError("team_restore_invalid")
        if extension["workspace_fingerprint"] != self.store.paths.workspace_fingerprint:
            raise TeamError("team_workspace_mismatch")
        proof = DeliveryProof.from_dict(extension["proof"])
        if (proof.team_id, proof.member_id, proof.session_id) != (self.team_id, self.member_id, session_id) or proof.generation > self.generation:
            raise TeamError("team_scope_invalid")
        errors, messages = [], []
        for line in lines[1:]:
            try:
                message = _parse_message_record(line.decode("utf-8"))
            except (UnicodeError, ValueError, TypeError):
                message = None
            if message is None:
                if "session_record_invalid" not in errors:
                    errors.append("session_record_invalid")
            else:
                messages.append(message)
        retained, truncated = _truncate_incomplete_tool_exchange(messages)
        if truncated:
            errors.append("session_tool_pair_truncated")
        body = b"".join(lines[1:])
        intact = not errors and extension["message_digest"] == hashlib.sha256(body).hexdigest() and extension["proof_digest"] == hashlib.sha256(encode(proof.to_dict()) + body).hexdigest()
        if not intact:
            proof = None
            errors.append("team_ack_checkpoint_required")
        # 恢复数据再次脱敏；返回的是新 ChatSession，无 Permission/session allow/运行对象。
        restored_messages = []
        for message in retained:
            record = sanitize(_message_record(message, self.store.sensitive_values), self.store.sensitive_values)
            parsed = _parse_message_record(encode(record).decode("utf-8"))
            if parsed is None:
                raise TeamError("team_restore_invalid")
            restored_messages.append(parsed)
        estimated = estimate_messages(restored_messages)
        return CheckpointRestore(ChatSession(restored_messages, session_id), proof, tuple(errors), truncated, estimated,
                                 estimated >= 51_000, now - updated >= TIME_SPAN_REMINDER_AGE)


def _has_delivery(messages: list[ChatMessage], envelope: dict) -> bool:
    expected = delivery_message(envelope)
    read_calls = set()
    for message in messages:
        if message == expected:
            return True
        for call in message.tool_calls or ():
            if call.name == "team_message" and call.arguments.get("operation") == "read":
                read_calls.add(call.id)
        if message.role == "tool" and message.tool_call_id in read_calls:
            try:
                result = json.loads(message.content or "")
                if result.get("ok") is True and result.get("tool_name") == "team_message" and envelope in result.get("data", {}).get("messages", []):
                    return True
            except (ValueError, TypeError, AttributeError):
                pass
    return False


def _utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# 不接收父 session、父 PermissionManager、MemoryService 或 HookEngine。
from newcode.agent import AgentLoop, AgentLoopConfig, AgentFinalAnswer, AgentStopped
from newcode.agent.mode import AgentMode, PLAN_TOOL_NAMES
from newcode.context.manager import ContextManager
from newcode.permissions.manager import PermissionManager
from newcode.permissions.confirmer import DenyByDefaultConfirmer
from newcode.permissions.rules import PermissionRuleSet
from newcode.permissions.types import PermissionMode, PermissionLayer, PermissionDecisionValue
from newcode.providers.base import ProviderError
from newcode.skills.state import ActiveSkillState
from newcode.skills.types import SkillCatalog
from newcode.subagents.policy import isolated_registry_view, most_restrictive_permission_mode
from newcode.subagents.runner import ChildReadCache
from newcode.subagents.types import AgentIsolation, TaskBudget
from newcode.tools.types import ToolCall, ToolContext, ToolFailure
from newcode.worktrees.types import WorktreeState
from .types import (MemberState, MessageKind, PlanState, RecordChange, TeamMemberRecord,
                    TeamPolicySnapshot, TeamRole, new_id, validate_policy_update)
from .runtime import StoreExecutorLeases


@dataclass(frozen=True)
class MemberPermissions:
    """可信配置的不可变规则副本；故意没有 session allow 或 confirmer 端口。"""
    mode: PermissionMode = PermissionMode.DEFAULT
    local_rules: tuple = ()
    project_rules: tuple = ()
    user_rules: tuple = ()
    rule_load_errors: tuple = ()
    session_denies: tuple = ()

    def __post_init__(self):
        for name in ("local_rules", "project_rules", "user_rules", "rule_load_errors", "session_denies"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if any(rule.action is not PermissionDecisionValue.DENY for rule in self.session_denies):
            raise TeamError("team_policy_changed")

    def build(self, requested):
        return PermissionManager(
            mode=most_restrictive_permission_mode(self.mode, PermissionMode(requested.value)),
            session_rules=PermissionRuleSet(PermissionLayer.SESSION_RULES, self.session_denies),
            local_project_rules=PermissionRuleSet(PermissionLayer.LOCAL_PROJECT_RULES, self.local_rules),
            project_rules=PermissionRuleSet(PermissionLayer.PROJECT_RULES, self.project_rules),
            user_global_rules=PermissionRuleSet(PermissionLayer.USER_GLOBAL_RULES, self.user_rules),
            rule_load_errors=self.rule_load_errors,
            confirmer=DenyByDefaultConfirmer())


@dataclass(frozen=True)
class MemberRunResult:
    natural: bool = False
    summary: str = ""
    error_code: str | None = None


class _MemberCancellation:
    """成员暂停不写 backend 的停止信号；恢复不能撤销真正的 shutdown。"""
    def __init__(self):
        self.local = threading.Event()
        self.backend = None

    def is_set(self):
        return self.local.is_set() or (self.backend is not None and self.backend.is_set())

    def set(self):
        self.local.set()


class TeamMemberRunner:
    """一个成员的 owner；可注入 lightweight callback，idle 不占 worker。

    open/close 持有真实 executor + Worktree 活动锁。activate 不创建第二个
    执行者，close 不能把仍在执行的工具冒充为静止。Provider 仅在所有检查后创建。
    """
    def __init__(self, *, store, scope, definition, registry, provider_factory,
                 default_model, policy: TeamPolicySnapshot, worktree_manager,
                 permissions: MemberPermissions = MemberPermissions(), mode=AgentMode.DO,
                 enable_team_tools=False):
        if policy.scope != scope or definition.isolation is not AgentIsolation.WORKTREE:
            raise TeamError("team_definition_invalid")
        self.store, self.scope, self.definition = store, scope, definition
        self.registry = isolated_registry_view(
            registry, {"read_file", "find_files", "search_code", "write_file", "replace_in_file"}, {})
        self.factory, self.default_model = provider_factory, default_model
        self.worktrees, self.permissions, self.mode = worktree_manager, permissions, mode
        self._policy, self._policy_owner = policy, threading.get_ident()
        self._policy_lock, self._execution = threading.RLock(), threading.Lock()
        self._parent_permission_limits = ()
        self._parent_permission_managers = ()
        self._lifecycle = threading.RLock()
        self._owner = self._lease = self._provider = None
        self._resource_owner = None
        self.session = self.context = self.permission = self.skill_state = self.cache = None
        self.cancel = _MemberCancellation()
        self._closed = False
        self.last_result = None
        self._budget = None
        self.checkpoints = MemberCheckpoints(store, scope.team_id, scope.member_id, generation=scope.generation)
        self.team_runtime = None
        if enable_team_tools:
            from .runtime import MemberTeamRuntime
            from .tools import register_team_tools
            self.team_runtime = MemberTeamRuntime(self)
            register_team_tools(self.registry, self.team_runtime)

    def publish_policy(self, snapshot):
        if threading.get_ident() != self._policy_owner:
            raise TeamError("team_policy_changed")
        with self._policy_lock:
            validate_policy_update(self._policy, snapshot)
            self._policy = snapshot

    def publish_permissions(self, snapshot: MemberPermissions):
        """父主线程发布不可变权限上限；历史上限保留，后续 allow 不能恢复。"""
        if threading.get_ident() != self._policy_owner or not isinstance(snapshot, MemberPermissions):
            raise TeamError("team_policy_changed")
        with self._policy_lock:
            if snapshot in self._parent_permission_limits:
                return
            if len(self._parent_permission_limits) >= 128:
                self.cancel.set()
                raise TeamError("team_limit_exceeded")
            self._parent_permission_limits += (snapshot,)
            self._parent_permission_managers += (snapshot.build(snapshot.mode),)

    def check_parent_permissions(self, call, tool):
        for manager in self._parent_permission_managers:
            if manager.check(call, self.tool_context, tool).decision is not PermissionDecisionValue.ALLOW:
                raise ToolFailure("permission_denied", "父级权限上限未允许该操作。")

    def _member_locked(self, *, cleanup=False):
        view, transactions, _ = self.store._recover_locked(self.scope.team_id)
        member = TeamMemberRecord.from_dict(view["members"][self.scope.member_id])
        if (member.member_id != self.scope.member_id or member.role is not TeamRole.MEMBER
                or (member.state is MemberState.INTEGRATED and not cleanup)):
            raise TeamError("team_state_invalid")
        return member, view, transactions

    def _team_live(self, view):
        return view.get("team", {}).get(self.scope.team_id, {}).get("state") in {"ready", "active"}

    def _update_locked(self, member, view, **changes):
        updated = replace(member, revision=member.revision + 1, **changes)
        self.store._commit_locked(self.scope.team_id, view["team"][self.scope.team_id]["revision"],
                                 new_id("mutation"), (RecordChange("members", member.member_id, updated.to_dict()),), None)
        return updated

    def open(self, *, expected_revision=None):
        with self._lifecycle:
            if self._closed:
                raise TeamError("team_state_invalid")
            if self._owner is not None:
                return
            owner = StoreExecutorLeases(self.store).acquire(self.scope)
            lease = None
            context = None
            try:
                with self.store.lock(self.scope.team_id):
                    member, view, _ = self._member_locked()
                    if not self._team_live(view):
                        raise TeamError("team_state_invalid")
                    if expected_revision is not None and member.revision != expected_revision:
                        raise TeamError("team_revision_conflict")
                    if self.scope.generation <= member.runtime_generation:
                        raise TeamError("team_scope_invalid")
                    if member.checkpoint_session_id:
                        if member.checkpoint_session_id != self.scope.session_id:
                            raise TeamError("team_scope_invalid")
                        restored = self.checkpoints._restore_locked(self.scope.session_id, _utc(None))
                        # 损坏证明/过期/预算不确定均不创建 Provider；工具尾截断适配仍由恢复器提供。
                        if (restored.session is None or restored.proof is None or restored.errors
                                or restored.needs_compaction or restored.estimated_tokens >= 16_000):
                            raise TeamError("team_restore_invalid")
                        session = restored.session
                    elif member.state not in {MemberState.CREATED, MemberState.STARTING}:
                        raise TeamError("team_restore_invalid")
                    else:
                        if self.store.paths.checked(*self.checkpoints._parts(self.scope.session_id), missing=True).exists():
                            # 未被成员记录绑定的旧文件不作为新会话覆盖目标。
                            raise TeamError("team_restore_invalid")
                        session = ChatSession(session_id=self.scope.session_id)
                    if estimate_messages([ChatMessage("system", self.definition.body), *session.messages]) >= 16_000:
                        raise TeamError("team_restore_invalid")
                if self.worktrees is None:
                    raise TeamError("team_worktree_owner_invalid")
                lease = self.worktrees.claim_team_member(self.store, self.scope, self.definition.name)
                tool_context = ToolContext(workspace_root=lease.path, cwd=lease.path,
                                           sensitive_values=self.store.sensitive_values,
                                           worktree_task_id=lease.task_id)
                context = ContextManager(session, lease.path, self.store.sensitive_values,
                                         artifact_session_id=self.scope.session_id)
                permission = self.permissions.build(self.definition.permission_mode)
                model = self.definition.model or self.default_model
                if model not in frozenset(self.factory.available_models):
                    raise TeamError("team_backend_unavailable")
                # 首次 checkpoint 尚无邮件/模型副作用；发布 scope 后才接收任何工作。
                if member.checkpoint_session_id is None:
                    self.checkpoints.checkpoint(session)
                with self.store.lock(self.scope.team_id):
                    current, view, _ = self._member_locked()
                    if not self._team_live(view):
                        raise TeamError("team_state_invalid")
                    if expected_revision is not None and current.revision != member.revision:
                        raise TeamError("team_revision_conflict")
                    changes = {"runtime_generation": self.scope.generation,
                               "checkpoint_session_id": session.session_id}
                    if current.state in {MemberState.RUNNING, MemberState.AWAITING_APPROVAL, MemberState.RESUMING,
                                         MemberState.PAUSING, MemberState.STOPPING}:
                        changes.update(state=MemberState.INTERRUPTED, auto_wake=False)
                    self._update_locked(current, view, **changes)
                # 恢复前置条件全部通过，才允许构造独立 client。
                if self.cancel.is_set():
                    raise TeamError("team_scope_invalid")
                provider = self.factory.create(model, timeout_seconds=30)
                self.session, self.context, self.permission = session, context, permission
                self.skill_state, self.tool_context = ActiveSkillState(), tool_context
                self.cache = ChildReadCache(workspace_root=lease.path, workspace_identity=tool_context.workspace_identity)
                self._provider, self._lease, self._owner = provider, lease, owner
                self._resource_owner = (self.scope, owner, lease)
            except BaseException:
                if context is not None:
                    context.cleanup()
                if lease is not None:
                    self.worktrees.release(lease, WorktreeState.FAILED)
                owner.__exit__(None, None, None)
                raise

    def _allowed_locked(self, member, view):
        if (not self._team_live(view) or member.runtime_generation != self.scope.generation
                or member.state not in {MemberState.RUNNING, MemberState.AWAITING_APPROVAL}):
            return frozenset()
        allowed = (frozenset(self.registry.names()) & self._policy.startup_tools & self._policy.current_tools
                   & frozenset(self.definition.tools_allow) - frozenset(self.definition.tools_deny))
        # 本阶段没有经验证的命令/网络隔离，也不提供嵌套或 Skill runner 入口。
        allowed &= {"read_file", "find_files", "search_code", "write_file", "replace_in_file"}
        if self.mode is AgentMode.PLAN:
            allowed &= PLAN_TOOL_NAMES
        if not self._approval_valid(member, view):
            allowed &= PLAN_TOOL_NAMES
        if self.skill_state.activations:
            for active in self.skill_state.activations:
                allowed &= frozenset(active.loaded.metadata.frontmatter.tools)
        if self.team_runtime is not None:
            from .tools import MEMBER_TOOLS
            role_tools = MEMBER_TOOLS & self._policy.startup_tools & self._policy.current_tools
            role_tools -= frozenset(self.definition.tools_deny)
            if self.mode is AgentMode.PLAN:
                role_tools &= {"team_status"}
            for active in self.skill_state.activations:
                role_tools &= frozenset(active.loaded.metadata.frontmatter.tools)
            allowed |= role_tools
        return frozenset(allowed)

    def _approval_valid(self, member, view):
        """状态恢复与工具准入使用同一 At 判定；批准不改变 P0/Pt 或 Permission。"""
        if not member.requires_plan_approval:
            return True
        policy = self._policy
        plans = [p for p in view.get("plans", {}).values()
                 if p["member_id"] == member.member_id and p["state"] != "superseded"]
        return bool(plans and policy.plan_state is PlanState.APPROVED
                    and policy.plan_revision == member.plan_revision
                    and all(p["state"] == "approved" for p in plans))

    def _check_budget(self):
        if (self._closed or self.cancel.is_set() or self._budget is None
                or not self._budget.check(cancelled=self.cancel.is_set())):
            raise TeamError("team_worker_failed")

    def activate(self, prompt="继续处理分配的任务与收件箱。", *, cancel_event=None, deadline=None):
        if not self._execution.acquire(blocking=False):
            raise TeamError("team_member_busy")
        try:
            if cancel_event is not None:
                self.cancel.backend = cancel_event
            if self.cancel.is_set():
                raise TeamError("team_state_invalid")
            self.open()
            self.worktrees.verify_team_member(self.store, self.scope, self._lease)
            self._budget = TaskBudget(max_rounds=min(8, self.definition.max_iterations),
                                      max_seconds=max(0.001, min(300, (deadline or time.monotonic()+300)-time.monotonic())),
                                      max_tokens=16_000)
            with self.store.lock(self.scope.team_id):
                member, view, _ = self._member_locked()
                if not self._team_live(view):
                    raise TeamError("team_state_invalid")
                if not member.auto_wake:
                    raise TeamError("team_state_invalid")
                if member.state in {MemberState.IDLE, MemberState.PAUSED}:
                    self._update_locked(member, view, state=MemberState.RESUMING)
                elif member.state in {MemberState.CREATED, MemberState.INTERRUPTED, MemberState.FAILED}:
                    self._update_locked(member, view, state=MemberState.STARTING)
                member, view, _ = self._member_locked()
                target = MemberState.RUNNING if self._approval_valid(member, view) else MemberState.AWAITING_APPROVAL
                if member.state in {MemberState.STARTING, MemberState.RESUMING}:
                    self._update_locked(member, view, state=target)
            from .mailbox import MailboxService
            MailboxService(self.store, self.scope.team_id, self.scope.member_id).deliver_pending(self.session, self.checkpoints)
            wrappers = {name: _MemberTool(self, self.registry.get(name)) for name in self.registry.names()
                        if name in {"read_file", "find_files", "search_code", "write_file", "replace_in_file"}}
            if self.team_runtime is not None:
                from .tools import MEMBER_TOOLS
                wrappers.update({name: self.registry.get(name) for name in MEMBER_TOOLS})
            view = isolated_registry_view(self.registry, wrappers, wrappers)
            if self.team_runtime is not None:
                from .tools import register_team_tools
                register_team_tools(view, self.team_runtime)
            loop = AgentLoop(provider=_MemberProvider(self), session=self.session, registry=view,
                             tool_context=self.tool_context, permission_manager=self.permission,
                             context_manager=self.context, config=AgentLoopConfig(max_iterations=min(8, self.definition.max_iterations)),
                             skill_state=self.skill_state, skill_catalog=SkillCatalog(()),
                             memory_service=None, hook_engine=None, team_runtime=self.team_runtime)
            final = None
            for event in loop.run(prompt, mode=self.mode, cancel_flag=self.cancel):
                if isinstance(event, AgentFinalAnswer):
                    final = event.content
                elif isinstance(event, AgentStopped):
                    final = None
            if final is None or self.cancel.is_set():
                raise TeamError("team_worker_failed")
            self._finish_idle()
            result = MemberRunResult(True, sanitize(final, self.store.sensitive_values)[:4000])
            self.last_result = result
            return result
        except Exception as exc:
            code = exc.code if isinstance(exc, TeamError) else "team_worker_failed"
            self.last_result = MemberRunResult(error_code=code)
            if self._owner is not None:
                try:
                    # 工具/请求已真实返回才处理暂停；不把 cancel flag 当作静止证明。
                    with self.store.lock(self.scope.team_id):
                        member, _, _ = self._member_locked()
                    if member.state is MemberState.PAUSING:
                        self.checkpoints.checkpoint(self.session)
                    with self.store.lock(self.scope.team_id):
                        member, view, _ = self._member_locked()
                        if member.state is MemberState.PAUSING:
                            self._update_locked(member, view, state=MemberState.PAUSED, auto_wake=False)
                        elif member.state in {MemberState.RUNNING, MemberState.AWAITING_APPROVAL, MemberState.STARTING, MemberState.RESUMING, MemberState.STOPPING}:
                            target = MemberState.INTERRUPTED if self.cancel.is_set() else MemberState.FAILED
                            self._update_locked(member, view, state=target, auto_wake=False)
                except Exception:
                    pass
            raise TeamError(code) from None
        finally:
            self._execution.release()

    def __call__(self, activation):
        if activation.scope != self.scope:
            raise TeamError("team_scope_invalid")
        try:
            try:
                self.activate(cancel_event=activation.cancel_event, deadline=activation.deadline)
            except TeamError:
                with self.store.lock(self.scope.team_id):
                    member, _, _ = self._member_locked()
                if member.state is not MemberState.PAUSED or activation.cancel_event.is_set():
                    raise
        finally:
            if activation.cancel_event.is_set():
                self.close()

    def _finish_idle(self):
        from .mailbox import make_system_message
        # 不把只存在内存的 final 冒充持久完成。故障后旧 checkpoint 仍可恢复。
        proof = self._checkpoint_observations()
        self.store._fault("member_after_final_checkpoint")
        with self.store.lock(self.scope.team_id):
            member, view, transactions = self._member_locked()
            if member.state is MemberState.IDLE:
                return
            if member.state not in {MemberState.RUNNING, MemberState.AWAITING_APPROVAL}:
                raise TeamError("team_state_invalid")
            updated = replace(member, state=MemberState.IDLE, revision=member.revision+1,
                              checkpoint_session_id=proof.session_id,
                              input_tokens=member.input_tokens+self._budget.input_tokens,
                              output_tokens=member.output_tokens+self._budget.output_tokens)
            lead = view["team"][self.scope.team_id]["lead_member_id"]
            sequence = max((v["sequence"] for v in view.get("messages", {}).values()), default=0)+1
            message = make_system_message(self.scope.team_id, self.scope.member_id, lead, MessageKind.MEMBER_IDLE,
                                          {"member_id": self.scope.member_id, "session_id": proof.session_id,
                                           "generation": self.scope.generation}, sequence)
            self.store._commit_locked(self.scope.team_id, transactions[-1].revision, new_id("mutation"),
                                      (RecordChange("members", updated.member_id, updated.to_dict()),
                                       RecordChange("messages", message.message_id, message.to_dict())), None)

    def _checkpoint_observations(self):
        """保存真实 read 观察的证明，但模型 read 本身绝不隐式 ack。"""
        with self.store.lock(self.scope.team_id):
            _, view, _ = self._member_locked()
            observed = tuple(sorted(message_id for message_id, message in view.get("messages", {}).items()
                                    if message["recipient"] == self.scope.member_id
                                    and _has_delivery(self.session.messages, message)))
        return self.checkpoints.checkpoint(self.session, processed_message_ids=observed)

    def pause(self, *, expected_revision=None):
        with self.store.lock(self.scope.team_id):
            member, view, _ = self._member_locked()
            if expected_revision is not None and member.revision != expected_revision:
                raise TeamError("team_revision_conflict")
            if expected_revision is not None and member.state not in {MemberState.RUNNING, MemberState.AWAITING_APPROVAL, MemberState.PAUSING, MemberState.PAUSED}:
                raise TeamError("team_state_invalid")
            changes = {"auto_wake": False}
            if member.state in {MemberState.RUNNING, MemberState.AWAITING_APPROVAL}:
                changes["state"] = MemberState.PAUSING
            self._update_locked(member, view, **changes)
            self.cancel.set()

    def stop(self, *, expected_revision):
        with self.store.lock(self.scope.team_id):
            member, view, _ = self._member_locked()
            if member.revision != expected_revision:
                raise TeamError("team_revision_conflict")
            if member.state is not MemberState.STOPPING:
                self._update_locked(member, view, state=MemberState.STOPPING, auto_wake=False)
            self.cancel.set()

    def resume(self, *, expected_revision=None):
        if not self._execution.acquire(blocking=False):
            raise TeamError("team_member_busy")
        try:
            existing = self._owner is not None
            self.open(expected_revision=expected_revision)
            self.worktrees.verify_team_member(self.store, self.scope, self._lease)
            with self.store.lock(self.scope.team_id):
                member, view, _ = self._member_locked()
                if existing and expected_revision is not None and member.revision != expected_revision:
                    raise TeamError("team_revision_conflict")
                restored = self.checkpoints._restore_locked(self.scope.session_id, _utc(None))
                if restored.session is None or restored.proof is None or restored.errors or restored.estimated_tokens >= 16_000:
                    raise TeamError("team_restore_invalid")
                self.session.replace_messages(restored.session.messages)
                changes = {"auto_wake": True}
                if member.state in {MemberState.IDLE, MemberState.PAUSED}:
                    changes["state"] = MemberState.RESUMING
                elif member.state in {MemberState.FAILED, MemberState.INTERRUPTED}:
                    changes["state"] = MemberState.STARTING
                self._update_locked(member, view, **changes)
            self.cancel = _MemberCancellation()
        finally:
            self._execution.release()

    def wake(self, enqueue):
        """邮箱唤醒端口：暂停后只保留邮件，不绕过显式 resume。"""
        with self.store.lock(self.scope.team_id):
            member, _, _ = self._member_locked()
            if not member.auto_wake or member.state not in {MemberState.IDLE, MemberState.RUNNING, MemberState.AWAITING_APPROVAL,
                                                          MemberState.RESUMING, MemberState.STARTING}:
                return False
        enqueue(self.scope)
        return True

    def quiescent(self):
        """仅用于状态显示；不是可跨操作使用的静止证明。"""
        acquired = self._execution.acquire(blocking=False)
        if acquired:
            self._execution.release()
        return acquired

    @contextmanager
    def quiescence(self):
        """可信服务的静止屏障：持有到调用者结束，期间禁止唤醒/接管。"""
        if not self._execution.acquire(blocking=False):
            raise TeamError("team_member_busy")
        try:
            if self._owner is None or self._closed:
                raise TeamError("team_state_invalid")
            yield self.scope
        finally:
            self._execution.release()

    def close(self):
        self.cancel.set()
        if not self._execution.acquire(blocking=False):
            return False
        try:
            with self._lifecycle:
                self._closed = True
                if all(resource is None for resource in (self.context, self.cache, self._provider, self._lease, self._owner)):
                    return True
                binding = self._resource_owner
                if (binding is None or binding[0] != self.scope
                        or (self._owner is not None and binding[1] is not self._owner)
                        or (self._lease is not None and binding[2] is not self._lease)):
                    return False
                # artifact 路径会在恢复时复用：清理失败后重试也须重新取得 owner。
                with ExitStack() as stack:
                    try:
                        if self._owner is None:
                            temporary_owner = StoreExecutorLeases(self.store).acquire(self.scope)
                            stack.callback(temporary_owner.__exit__, None, None, None)
                        stack.enter_context(self.store.lock(self.scope.team_id))
                        member, view, _ = self._member_locked(cleanup=True)
                        if member.runtime_generation != self.scope.generation:
                            return False
                    except Exception:
                        return False
                    return self._close_resources_locked(member, view)
        except Exception:
            return False
        finally:
            self._execution.release()

    def _close_resources_locked(self, member, view):
        """调用方持有 execution、lifecycle、Team 与本人 native owner。"""
        failed = False
        if member.state is MemberState.STOPPING:
            try:
                self._update_locked(member, view, state=MemberState.INTERRUPTED, auto_wake=False)
            except Exception:
                failed = True
        # 本地资源互不依赖；失败的资源保留引用，允许下一次 close 重试。
        for name, method in (("context", "cleanup"), ("cache", "invalidate")):
            resource = getattr(self, name)
            if resource is not None:
                try:
                    getattr(resource, method)()
                    setattr(self, name, None)
                except Exception:
                    failed = True
        if self._provider is not None:
            close = getattr(self._provider, "close", None)
            try:
                if callable(close):
                    close()
                self._provider = None
            except Exception:
                failed = True
        if self._lease is not None:
            try:
                self.worktrees.release(self._lease, WorktreeState.COMPLETED)
                self._lease = None
            except Exception:
                failed = True
        if self._owner is not None:
            try:
                self._owner.__exit__(None, None, None)
                self._owner = None
            except Exception:
                failed = True
        return not failed


class _MemberProvider:
    def __init__(self, runner):
        self.runner = runner

    def stream_chat(self, messages, tools=None, allow_tool_calls=True):
        runner = self.runner
        try:
            runner._check_budget()
            # 每次请求前保存模型 read 的观察，但不替模型 ack。
            runner._checkpoint_observations()
            with runner._policy_lock, runner.store.lock(runner.scope.team_id):
                member, view, _ = runner._member_locked()
                if not runner._team_live(view):
                    raise TeamError("team_state_invalid")
                allowed = runner._allowed_locked(member, view)
                filtered = [t for t in tools or () if t.get("function", {}).get("name") in allowed]
            request = []
            for message in [ChatMessage("system", runner.definition.body), *messages]:
                record = sanitize(_message_record(message, runner.store.sensitive_values), runner.store.sensitive_values)
                parsed = _parse_message_record(encode(record).decode("utf-8"))
                if parsed is None:
                    raise TeamError("team_restore_invalid")
                request.append(parsed)
            request_text = str([(m.role, m.content, m.tool_calls) for m in request]) + encode(filtered).decode("utf-8")
            if not runner._budget.begin_round(request_text, cancelled=runner.cancel.is_set()):
                raise TeamError("team_limit_exceeded")
            for event in runner._provider.stream_chat(request, tools=filtered, allow_tool_calls=allow_tool_calls):
                runner._check_budget()
                if hasattr(event, "text") and not runner._budget.record_output(event.text):
                    raise TeamError("team_limit_exceeded")
                if hasattr(event, "tool_calls"):
                    output = encode([{"name": call.name, "arguments": call.arguments,
                                      "raw_arguments": call.raw_arguments} for call in event.tool_calls]).decode("utf-8")
                    if not runner._budget.record_output(output):
                        raise TeamError("team_limit_exceeded")
                yield event
        except Exception:
            raise ProviderError("team_worker_failed") from None


class _MemberTool:
    def __init__(self, runner, inner):
        self.runner, self.inner, self.spec = runner, inner, inner.spec

    def run(self, arguments, context):
        runner = self.runner
        # 版本屏障覆盖实际工具执行；审批服务使用同一 Team 锁，无法在检查后失效。
        with runner._policy_lock, runner.store.lock(runner.scope.team_id):
            runner._check_budget()
            member, view, _ = runner._member_locked()
            if self.spec.name not in runner._allowed_locked(member, view):
                raise ToolFailure("disallowed_tool", "成员策略不允许该操作。")
            call = ToolCall("member-check", self.spec.name, dict(arguments))
            runner.check_parent_permissions(call, self.inner)
            decision = runner.permission.check(call, runner.tool_context, self.inner)
            if decision.decision is not PermissionDecisionValue.ALLOW:
                raise ToolFailure("permission_denied", "成员权限未允许该操作。")
            return self.inner.run(dict(arguments), runner.tool_context)
