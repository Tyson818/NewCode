"""进程内有界 SubAgent 任务记录与注入式 worker 调度。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import secrets
import threading
import time
from typing import Callable

from .types import (
    MAX_TASK_SECONDS,
    MAX_TASK_SUMMARY_CHARS,
    MAX_TASK_TOKENS,
    ParentPolicySnapshot,
    TERMINAL_TASK_STATES,
    SessionScope,
    SubAgentManagerError,
    TaskBudget,
    TaskExecution,
    TaskResult,
    TaskSnapshot,
    TaskState,
    WorkerResult,
    WorkerTaskContext,
)


MAX_WORKERS = 2
MAX_QUEUED_TASKS = 4
MAX_SESSION_TASKS = 6
MAX_TASK_RECORDS = 128
MAX_WAIT_SECONDS = 30.0
MAX_SHUTDOWN_WAIT_SECONDS = 1.0

WorkerCallback = Callable[[WorkerTaskContext], WorkerResult]


@dataclass(frozen=True)
class WaitOutcome:
    snapshot: TaskSnapshot
    result: TaskResult | None
    timed_out: bool


@dataclass
class _TaskRecord:
    task_id: str
    scope: SessionScope
    task_input: str
    state: TaskState
    execution: TaskExecution
    budget: TaskBudget
    cancel_event: threading.Event
    background_requested: bool = False
    notification_pending: bool = False
    result_claimed: bool = False
    error_code: str | None = None
    summary: str = ""
    completion_sequence: int | None = None
    scope_closed: bool = False
    launch_policy: ParentPolicySnapshot | None = None
    payload: object | None = None


class SubAgentManager:
    """有界 Manager；任务执行逻辑只能经构造时注入的 worker callback。"""

    def __init__(
        self,
        worker: WorkerCallback,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(worker):
            raise TypeError("worker_callback_required")
        self._worker = worker
        self._clock = clock
        self._policy_owner_thread_id = threading.get_ident()
        self._condition = threading.Condition(threading.RLock())
        self._tasks: dict[str, _TaskRecord] = {}
        self._queue: deque[str] = deque()
        self._active_scopes: dict[str, SessionScope] = {}
        self._policy_snapshots: dict[SessionScope, ParentPolicySnapshot] = {}
        self._next_generation = 0
        self._next_completion_sequence = 0
        self._shutdown = False
        self._workers = [
            threading.Thread(target=self._worker_loop, name=f"newcode-subagent-{index}", daemon=True)
            for index in range(MAX_WORKERS)
        ]
        for thread in self._workers:
            thread.start()

    def __enter__(self) -> "SubAgentManager":
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()

    def open_session(self, session_id: str) -> SessionScope:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise SubAgentManagerError("subagent_invalid_request")
        with self._condition:
            self._ensure_running_locked()
            old_scope = self._active_scopes.get(session_id)
            if old_scope is not None:
                self._close_session_locked(old_scope)
            self._next_generation += 1
            scope = SessionScope(session_id, self._next_generation)
            self._active_scopes[session_id] = scope
            self._policy_snapshots.pop(old_scope, None) if old_scope is not None else None
            self._condition.notify_all()
            return scope

    def start(
        self,
        scope: SessionScope,
        task_input: str,
        *,
        execution: TaskExecution | str = TaskExecution.FOREGROUND,
        payload: object | None = None,
    ) -> TaskSnapshot:
        if not isinstance(task_input, str) or not task_input.strip():
            raise SubAgentManagerError("subagent_invalid_request")
        try:
            execution = TaskExecution(execution)
        except (TypeError, ValueError) as exc:
            raise SubAgentManagerError("subagent_invalid_request") from exc
        if _estimate_tokens(task_input) > MAX_TASK_TOKENS:
            raise SubAgentManagerError("subagent_token_budget_exceeded")

        with self._condition:
            self._expire_due_locked()
            self._require_active_scope_locked(scope)
            self._ensure_record_capacity_locked()
            session_count = sum(
                1
                for record in self._tasks.values()
                if record.scope == scope and (record.state not in TERMINAL_TASK_STATES or not record.result_claimed)
            )
            if session_count >= MAX_SESSION_TASKS:
                raise SubAgentManagerError("subagent_concurrency_limit")
            if sum(record.state is TaskState.QUEUED for record in self._tasks.values()) >= MAX_QUEUED_TASKS:
                raise SubAgentManagerError("subagent_queue_full")

            task_id = secrets.token_urlsafe(18)
            while task_id in self._tasks:
                task_id = secrets.token_urlsafe(18)
            record = _TaskRecord(
                task_id=task_id,
                scope=scope,
                task_input=task_input,
                state=TaskState.QUEUED,
                execution=execution,
                budget=TaskBudget(clock=self._clock),
                cancel_event=threading.Event(),
                background_requested=execution is TaskExecution.BACKGROUND,
                launch_policy=self._policy_snapshots.get(scope),
                payload=payload,
            )
            self._tasks[task_id] = record
            self._queue.append(task_id)
            self._condition.notify()
            return self._snapshot(record)

    def publish_policy_snapshot(self, scope: SessionScope, snapshot: ParentPolicySnapshot) -> None:
        """由主线程发布仅可收窄的策略快照；快照自身不可变。"""
        if threading.get_ident() != self._policy_owner_thread_id:
            raise SubAgentManagerError("subagent_policy_publish_thread_invalid")
        if not isinstance(snapshot, ParentPolicySnapshot) or snapshot.scope != scope:
            raise SubAgentManagerError("subagent_policy_invalid")
        with self._condition:
            self._require_active_scope_locked(scope)
            previous = self._policy_snapshots.get(scope)
            if previous is not None:
                old_rank = _permission_restrictiveness(previous.permission_mode)
                new_rank = _permission_restrictiveness(snapshot.permission_mode)
                if (
                    not snapshot.visible_tools.issubset(previous.visible_tools)
                    or new_rank > old_rank
                    or not set(previous.permission_deny_rules).issubset(snapshot.permission_deny_rules)
                ):
                    raise SubAgentManagerError("subagent_policy_expansion_rejected")
                if new_rank < 0:
                    raise SubAgentManagerError("subagent_policy_invalid")
                snapshot = ParentPolicySnapshot(
                    scope=scope,
                    visible_tools=frozenset(snapshot.visible_tools),
                    permission_mode=snapshot.permission_mode,
                    revision=previous.revision + 1,
                    permission_deny_rules=snapshot.permission_deny_rules,
                )
            else:
                snapshot = ParentPolicySnapshot(
                    scope=scope,
                    visible_tools=frozenset(snapshot.visible_tools),
                    permission_mode=snapshot.permission_mode,
                    revision=0,
                    permission_deny_rules=snapshot.permission_deny_rules,
                )
            self._policy_snapshots[scope] = snapshot
            self._condition.notify_all()

    def policy_snapshot(self, scope: SessionScope) -> ParentPolicySnapshot:
        with self._condition:
            self._require_active_scope_locked(scope)
            snapshot = self._policy_snapshots.get(scope)
            if snapshot is None:
                raise SubAgentManagerError("subagent_policy_unavailable")
            return snapshot

    def status(self, scope: SessionScope, task_id: str) -> TaskSnapshot:
        with self._condition:
            self._expire_due_locked()
            return self._snapshot(self._get_record_locked(scope, task_id))

    def wait(self, scope: SessionScope, task_id: str, timeout_seconds: float = MAX_WAIT_SECONDS) -> WaitOutcome:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 <= timeout_seconds <= MAX_WAIT_SECONDS:
            raise SubAgentManagerError("subagent_invalid_request")
        deadline = time.monotonic() + float(timeout_seconds)
        with self._condition:
            record = self._get_record_locked(scope, task_id)
            while True:
                self._expire_due_locked()
                record = self._get_record_locked(scope, task_id)
                if record.state in TERMINAL_TASK_STATES:
                    return WaitOutcome(self._snapshot(record), self._claim_result_locked(record), False)

                remaining = deadline - time.monotonic()
                task_remaining = record.budget.remaining_seconds()
                if remaining <= 0:
                    self._request_background_locked(record)
                    return WaitOutcome(self._snapshot(record), None, True)
                if task_remaining <= 0:
                    self._expire_record_locked(record)
                    continue
                self._condition.wait(min(remaining, task_remaining))

    def background(self, scope: SessionScope, task_id: str) -> TaskSnapshot:
        with self._condition:
            self._expire_due_locked()
            record = self._get_record_locked(scope, task_id)
            if record.state in {TaskState.QUEUED, TaskState.RUNNING}:
                self._request_background_locked(record)
                self._condition.notify_all()
            return self._snapshot(record)

    def cancel(self, scope: SessionScope, task_id: str) -> TaskSnapshot:
        with self._condition:
            self._expire_due_locked()
            record = self._get_record_locked(scope, task_id)
            if record.state not in TERMINAL_TASK_STATES:
                record.cancel_event.set()
                self._remove_queued_locked(record)
                self._finish_locked(record, TaskState.CANCELLED, "subagent_cancelled")
            return self._snapshot(record)

    def collect(self, scope: SessionScope, task_id: str) -> TaskResult:
        with self._condition:
            self._expire_due_locked()
            record = self._get_record_locked(scope, task_id)
            if record.state not in TERMINAL_TASK_STATES:
                raise SubAgentManagerError("subagent_result_not_ready")
            return self._claim_result_locked(record)

    def drain_notifications(self, scope: SessionScope, *, limit: int = MAX_TASK_RECORDS) -> tuple[TaskResult, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise SubAgentManagerError("subagent_invalid_request")
        with self._condition:
            self._expire_due_locked()
            if self._active_scopes.get(scope.session_id) != scope:
                return ()
            pending = sorted(
                (
                    record
                    for record in self._tasks.values()
                    if record.scope == scope
                    and record.notification_pending
                    and record.state in TERMINAL_TASK_STATES
                    and not record.result_claimed
                ),
                key=lambda item: item.completion_sequence or 0,
            )[: min(limit, MAX_TASK_RECORDS)]
            results = tuple(self._claim_result_locked(record) for record in pending)
            self._condition.notify_all()
            return results

    def close_session(self, scope: SessionScope) -> None:
        with self._condition:
            self._close_session_locked(scope)
            self._condition.notify_all()

    def check_deadlines(self) -> None:
        """Fake-clock friendly deadline sweep; normal queries also sweep deadlines."""
        with self._condition:
            self._expire_due_locked()
            self._condition.notify_all()

    def shutdown(self, wait_timeout: float = MAX_SHUTDOWN_WAIT_SECONDS) -> None:
        if isinstance(wait_timeout, bool) or not isinstance(wait_timeout, (int, float)) or not 0 <= wait_timeout <= MAX_SHUTDOWN_WAIT_SECONDS:
            wait_timeout = MAX_SHUTDOWN_WAIT_SECONDS
        with self._condition:
            if not self._shutdown:
                self._shutdown = True
                for scope in tuple(self._active_scopes.values()):
                    self._close_session_locked(scope)
                self._queue.clear()
                self._condition.notify_all()

        deadline = time.monotonic() + float(wait_timeout)
        current = threading.current_thread()
        for thread in self._workers:
            if thread is current:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                thread.join(remaining)
            except RuntimeError:
                # shutdown 在 interpreter/thread teardown 時保持 best-effort。
                continue

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._shutdown:
                    self._condition.wait()
                if self._shutdown and not self._queue:
                    return
                task_id = self._queue.popleft()
                record = self._tasks.get(task_id)
                if record is None or record.state is not TaskState.QUEUED:
                    continue
                if record.budget.remaining_seconds() <= 0:
                    self._expire_record_locked(record)
                    continue
                record.state = TaskState.RUNNING
                if record.background_requested:
                    record.state = TaskState.BACKGROUND
                    record.execution = TaskExecution.BACKGROUND
                context = WorkerTaskContext(
                    task_id=record.task_id,
                    scope=record.scope,
                    task_input=record.task_input,
                    cancel_event=record.cancel_event,
                    budget=record.budget,
                    launch_policy=record.launch_policy,
                    payload=record.payload,
                )
                self._condition.notify_all()
            try:
                result = self._worker(context)
                if not isinstance(result, WorkerResult):
                    result = WorkerResult(error_code="subagent_provider_error")
            except BaseException:
                # 注入 worker 故障不得逃出 daemon thread 或泄露 exception 文本。
                result = WorkerResult(error_code="subagent_provider_error")
            with self._condition:
                current = self._tasks.get(task_id)
                if current is not record or record.state in TERMINAL_TASK_STATES:
                    continue
                self._expire_due_locked()
                if record.state in TERMINAL_TASK_STATES:
                    continue
                stop_code = record.budget.stop_code
                if stop_code == "subagent_timeout":
                    self._finish_locked(record, TaskState.TIMED_OUT, stop_code)
                elif stop_code == "subagent_cancelled":
                    self._finish_locked(record, TaskState.CANCELLED, stop_code)
                elif stop_code is not None:
                    self._finish_locked(record, TaskState.FAILED, stop_code)
                elif result.error_code is not None:
                    code = result.error_code if result.error_code in _SAFE_WORKER_ERROR_CODES else "subagent_provider_error"
                    state = TaskState.TIMED_OUT if code == "subagent_timeout" else TaskState.CANCELLED if code == "subagent_cancelled" else TaskState.FAILED
                    self._finish_locked(record, state, code)
                else:
                    summary = result.summary if isinstance(result.summary, str) else ""
                    self._finish_locked(record, TaskState.COMPLETED, None, summary[:MAX_TASK_SUMMARY_CHARS])
                self._condition.notify_all()

    def _finish_locked(
        self,
        record: _TaskRecord,
        state: TaskState,
        error_code: str | None,
        summary: str = "",
    ) -> None:
        if record.state in TERMINAL_TASK_STATES:
            return
        record.state = state
        record.error_code = error_code
        record.summary = summary
        self._next_completion_sequence += 1
        record.completion_sequence = self._next_completion_sequence
        record.notification_pending = record.execution is TaskExecution.BACKGROUND and not record.scope_closed

    def _request_background_locked(self, record: _TaskRecord) -> None:
        if record.state not in {TaskState.QUEUED, TaskState.RUNNING, TaskState.BACKGROUND}:
            return
        record.background_requested = True
        record.execution = TaskExecution.BACKGROUND
        if record.state is TaskState.RUNNING:
            record.state = TaskState.BACKGROUND

    def _claim_result_locked(self, record: _TaskRecord) -> TaskResult:
        if record.result_claimed:
            raise SubAgentManagerError("subagent_result_already_collected")
        record.result_claimed = True
        record.notification_pending = False
        return TaskResult(
            task_id=record.task_id,
            state=record.state,
            summary=record.summary[:MAX_TASK_SUMMARY_CHARS],
            error_code=record.error_code,
            completion_sequence=record.completion_sequence,
        )

    def _snapshot(self, record: _TaskRecord) -> TaskSnapshot:
        return TaskSnapshot(
            task_id=record.task_id,
            scope=record.scope,
            state=record.state,
            execution=record.execution,
            rounds=record.budget.rounds,
            input_tokens=record.budget.input_tokens,
            output_tokens=record.budget.output_tokens,
            error_code=record.error_code,
            result_available=record.state in TERMINAL_TASK_STATES and not record.result_claimed,
            completion_sequence=record.completion_sequence,
        )

    def _get_record_locked(self, scope: SessionScope, task_id: str) -> _TaskRecord:
        record = self._tasks.get(task_id)
        if record is None or record.scope != scope:
            raise SubAgentManagerError("subagent_not_found")
        if record.scope_closed or self._active_scopes.get(scope.session_id) != scope:
            raise SubAgentManagerError("subagent_parent_session_closed")
        return record

    def _require_active_scope_locked(self, scope: SessionScope) -> None:
        if not isinstance(scope, SessionScope) or self._active_scopes.get(scope.session_id) != scope:
            raise SubAgentManagerError("subagent_parent_session_closed")

    def _ensure_running_locked(self) -> None:
        if self._shutdown:
            raise SubAgentManagerError("subagent_parent_session_closed")

    def _ensure_record_capacity_locked(self) -> None:
        if len(self._tasks) < MAX_TASK_RECORDS:
            return
        eligible = sorted(
            (
                record
                for record in self._tasks.values()
                if record.state in TERMINAL_TASK_STATES and record.result_claimed
            ),
            key=lambda item: item.completion_sequence or 0,
        )
        if not eligible:
            raise SubAgentManagerError("subagent_task_store_full")
        self._tasks.pop(eligible[0].task_id, None)

    def _close_session_locked(self, scope: SessionScope) -> None:
        self._policy_snapshots.pop(scope, None)
        if self._active_scopes.get(scope.session_id) == scope:
            self._active_scopes.pop(scope.session_id, None)
        for record in self._tasks.values():
            if record.scope != scope:
                continue
            record.scope_closed = True
            if record.state not in TERMINAL_TASK_STATES:
                record.cancel_event.set()
                self._remove_queued_locked(record)
                self._finish_locked(record, TaskState.CANCELLED, "subagent_parent_session_closed")
            record.notification_pending = False
            record.summary = ""
            record.result_claimed = True

    def _expire_due_locked(self) -> None:
        for record in self._tasks.values():
            if record.state not in TERMINAL_TASK_STATES and record.budget.remaining_seconds() <= 0:
                self._expire_record_locked(record)

    def _expire_record_locked(self, record: _TaskRecord) -> None:
        if record.state in TERMINAL_TASK_STATES:
            return
        record.cancel_event.set()
        self._remove_queued_locked(record)
        self._finish_locked(record, TaskState.TIMED_OUT, "subagent_timeout")
        self._condition.notify_all()

    def _remove_queued_locked(self, record: _TaskRecord) -> None:
        if record.state is TaskState.QUEUED:
            try:
                self._queue.remove(record.task_id)
            except ValueError:
                pass


_SAFE_WORKER_ERROR_CODES = frozenset(
    {
        "subagent_definition_invalid",
        "subagent_model_unavailable",
        "subagent_permission_denied",
        "subagent_timeout",
        "subagent_cancelled",
        "subagent_iteration_limit",
        "subagent_token_budget_exceeded",
        "subagent_provider_error",
        "subagent_policy_unavailable",
        "subagent_worktree_unavailable",
        "subagent_worktree_setup_failed",
    }
)


def _estimate_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 2) // 3


def _permission_restrictiveness(mode) -> int:
    from newcode.permissions.types import PermissionMode

    return {
        PermissionMode.STRICT: 0,
        PermissionMode.DEFAULT: 1,
        PermissionMode.PERMISSIVE: 2,
        PermissionMode.TRUSTED: 2,
    }.get(mode, -1)
