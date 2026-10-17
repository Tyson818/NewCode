"""共享任务的受控 CRUD、CAS 与状态迁移。"""

from __future__ import annotations

from dataclasses import replace
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Callable

from .store import TeamStore
from .types import (
    MAX_TASKS, MAX_DEPENDENCIES, MessageKind, RecordChange, TaskState, TeamError,
    TeamRole, TeamTask, new_id, positive, validate_name, validate_transition, utc_now,
)


class TaskService:
    """通过 TeamStore 的同一 owner lock 完成验证与事务提交。

    ``actor_member_id`` 是调用方从可信运行时传入的身份；它不是任务字段可
    以覆盖的值。该服务本身不启动成员，也不负责任务调度。
    """

    def __init__(self, store: TeamStore, team_id: str, *, clock: Callable[[], str] = utc_now) -> None:
        self.store = store
        self.team_id = team_id
        self.clock = clock

    def list(self, actor_member_id: str) -> tuple[TeamTask, ...]:
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, _, _ = self.store._recover_locked(self.team_id)
            self._actor(view, actor_member_id)
            values = view.get("tasks", {})
            return tuple(TeamTask.from_dict(v) for _, v in sorted(values.items())
                         if not v.get("deleted", False))

    def get(self, actor_member_id: str, task_id: str) -> TeamTask:
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, _, _ = self.store._recover_locked(self.team_id)
            self._actor(view, actor_member_id)
            return self._get(view, task_id)

    def create(self, actor_member_id: str, *, title: str, description: str = "",
               dependencies: tuple[str, ...] = ()) -> TeamTask:
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, transactions, _ = self.store._recover_locked(self.team_id)
            actor = self._actor(view, actor_member_id)
            if actor.role is not TeamRole.LEAD:
                raise TeamError("team_role_denied")
            tasks = view.get("tasks", {})
            if len(tasks) >= MAX_TASKS:
                raise TeamError("team_limit_exceeded")
            self._validate_dependency_shape(dependencies)
            now = self.clock()
            task = TeamTask(new_id("task"), title, description, actor_member_id, now, now,
                            dependencies=tuple(dependencies))
            self._validate_dag(view, task.task_id, task.dependencies)
            self._commit(view, transactions, (RecordChange("tasks", task.task_id, task.to_dict()),))
            return task

    def claim(self, actor_member_id: str, task_id: str, *, expected_revision: int) -> TeamTask:
        positive(expected_revision)
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, transactions, _ = self.store._recover_locked(self.team_id)
            actor = self._actor(view, actor_member_id)
            if actor.role is not TeamRole.MEMBER:
                raise TeamError("team_role_denied")
            task = self._get(view, task_id)
            # 同一成员的重试为只读幂等返回，不会覆盖新的 revision。
            if task.state is TaskState.ASSIGNED and task.assignee == actor_member_id:
                return task
            self._expect(task, expected_revision)
            if task.state not in (TaskState.OPEN, TaskState.ASSIGNED):
                raise TeamError("team_task_conflict")
            if task.assignee is not None and task.assignee != actor_member_id:
                raise TeamError("team_task_conflict")
            self._ensure_dependencies_complete(view, task)
            updated = self._revise(task, state=TaskState.ASSIGNED, assignee=actor_member_id)
            event = self._task_event(view, transactions, actor_member_id, "lead", MessageKind.TASK_ASSIGNED, updated)
            self._commit(view, transactions, (RecordChange("tasks", task.task_id, updated.to_dict()), event))
            return updated

    def assign(self, actor_member_id: str, task_id: str, target_member_id: str, *,
               expected_revision: int) -> TeamTask:
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, transactions, _ = self.store._recover_locked(self.team_id)
            actor = self._actor(view, actor_member_id)
            if actor.role is not TeamRole.LEAD:
                raise TeamError("team_role_denied")
            target = self._actor(view, target_member_id)
            if target.role is not TeamRole.MEMBER:
                raise TeamError("team_role_denied")
            task = self._get(view, task_id)
            self._expect(task, expected_revision)
            if task.state not in (TaskState.OPEN, TaskState.ASSIGNED, TaskState.BLOCKED):
                raise TeamError("team_task_conflict")
            if target.state.value in {"starting", "running", "resuming", "pausing", "stopping"}:
                raise TeamError("team_member_busy")
            self._ensure_dependencies_complete(view, task)
            updated = self._revise(task, state=TaskState.ASSIGNED, assignee=target_member_id)
            event = self._task_event(view, transactions, actor_member_id, target_member_id, MessageKind.TASK_ASSIGNED, updated)
            self._commit(view, transactions, (RecordChange("tasks", task.task_id, updated.to_dict()), event))
            return updated

    def update(self, actor_member_id: str, task_id: str, *, expected_revision: int,
               changes: Mapping[str, object]) -> TeamTask:
        if not isinstance(changes, Mapping) or not changes or any(type(k) is not str for k in changes):
            raise TeamError("team_arguments_invalid")
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, transactions, _ = self.store._recover_locked(self.team_id)
            actor = self._actor(view, actor_member_id)
            task = self._get(view, task_id)
            self._expect(task, expected_revision)
            self._ensure_nonterminal(task)
            values = dict(changes)
            if actor.role is TeamRole.LEAD:
                allowed = {"title", "description", "dependencies", "state"}
                if set(values) - allowed:
                    raise TeamError("team_arguments_invalid")
                if set(values) & {"title", "description", "dependencies"} and task.state not in (TaskState.OPEN, TaskState.ASSIGNED):
                    raise TeamError("team_task_conflict")
            elif actor.role is TeamRole.MEMBER:
                if task.assignee != actor_member_id:
                    raise TeamError("team_role_denied")
                if set(values) != {"state"} or values["state"] not in (TaskState.IN_PROGRESS, TaskState.BLOCKED, "in_progress", "blocked"):
                    raise TeamError("team_arguments_invalid")
            else:
                raise TeamError("team_role_denied")
            if "dependencies" in values:
                self._validate_dependency_shape(values["dependencies"])
                values["dependencies"] = tuple(values["dependencies"])
                self._validate_dag(view, task.task_id, values["dependencies"])
            if "state" in values:
                try:
                    target_state = TaskState(values["state"])
                except (ValueError, TypeError):
                    raise TeamError("team_state_invalid") from None
                validate_transition(task.state, target_state)
                if target_state is TaskState.IN_PROGRESS:
                    self._ensure_dependencies_complete(view, task)
                values["state"] = target_state
            updated = self._revise(task, **values)
            if updated.state is TaskState.IN_PROGRESS:
                self._ensure_dependencies_complete(view, updated)
            self._commit(view, transactions, (RecordChange("tasks", task.task_id, updated.to_dict()),))
            return updated

    def complete(self, actor_member_id: str, task_id: str, *, expected_revision: int,
                 summary: str = "") -> TeamTask:
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, transactions, _ = self.store._recover_locked(self.team_id)
            actor = self._actor(view, actor_member_id)
            task = self._get(view, task_id)
            if actor.role is TeamRole.MEMBER and task.assignee != actor_member_id:
                raise TeamError("team_role_denied")
            if actor.role is TeamRole.LEAD and task.state is not TaskState.IN_PROGRESS:
                raise TeamError("team_task_conflict")
            self._expect(task, expected_revision)
            if task.state is not TaskState.IN_PROGRESS:
                raise TeamError("team_task_conflict")
            self._ensure_dependencies_complete(view, task)
            updated = self._revise(task, state=TaskState.COMPLETED, summary=summary)
            event = self._task_event(view, transactions, actor_member_id, "lead", MessageKind.TASK_COMPLETED, updated)
            self._commit(view, transactions, (RecordChange("tasks", task.task_id, updated.to_dict()), event))
            return updated

    def delete(self, actor_member_id: str, task_id: str, *, expected_revision: int) -> TeamTask:
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, transactions, _ = self.store._recover_locked(self.team_id)
            actor = self._actor(view, actor_member_id)
            if actor.role is not TeamRole.LEAD:
                raise TeamError("team_role_denied")
            task = self._get(view, task_id)
            self._expect(task, expected_revision)
            if task.state is not TaskState.OPEN or task.assignee is not None or self._has_dependents(view, task_id):
                raise TeamError("team_task_delete_denied")
            updated = self._revise(task, deleted=True)
            self._commit(view, transactions, (RecordChange("tasks", task.task_id, updated.to_dict()),))
            return updated

    def is_ready(self, task_id: str) -> bool:
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, _, _ = self.store._recover_locked(self.team_id)
            task = self._get(view, task_id)
            self._ensure_dependencies_complete(view, task)
            return True

    def _commit(self, view, transactions, changes, *, idempotency_key=None):
        expected = transactions[-1].revision if transactions else 0
        try:
            self.store._commit_locked(self.team_id, expected, new_id("mutation"), changes, idempotency_key)
        except TeamError as exc:
            if exc.code == "team_revision_conflict":
                raise TeamError("team_task_conflict") from None
            raise

    def _task_event(self, view, transactions, sender, recipient, kind, task):
        from .mailbox import make_system_message
        sequence = transactions[-1].message_high_watermark + 1 if transactions else 1
        message = make_system_message(
            self.team_id, sender, recipient, kind,
            {"task_id": task.task_id, "task_revision": task.revision, "state": task.state.value},
            sequence, now=self.clock(),
        )
        return RecordChange("messages", message.message_id, message.to_dict())

    def _actor(self, view, member_id: str):
        if not isinstance(member_id, str) or member_id not in view.get("members", {}):
            raise TeamError("team_scope_invalid")
        from .types import TeamMemberRecord
        return TeamMemberRecord.from_dict(view["members"][member_id])

    @staticmethod
    def _get(view, task_id: str) -> TeamTask:
        from .types import validate_id
        validate_id(task_id, "task")
        value = view.get("tasks", {}).get(task_id)
        if value is None or value.get("deleted", False):
            raise TeamError("team_task_not_found")
        return TeamTask.from_dict(value)

    @staticmethod
    def _expect(task: TeamTask, expected_revision: int) -> None:
        positive(expected_revision)
        if task.revision != expected_revision:
            raise TeamError("team_task_conflict")

    @staticmethod
    def _ensure_nonterminal(task: TeamTask) -> None:
        if task.state in (TaskState.COMPLETED, TaskState.CANCELLED):
            raise TeamError("team_task_conflict")

    @staticmethod
    def _revise(task: TeamTask, **changes) -> TeamTask:
        changes.setdefault("updated_at", utc_now())
        changes["revision"] = task.revision + 1
        return replace(task, **changes)

    @staticmethod
    def _validate_dependency_shape(dependencies) -> None:
        if not isinstance(dependencies, (tuple, list)) or len(dependencies) > MAX_DEPENDENCIES:
            raise TeamError("team_task_dependency_invalid")
        from .types import validate_id
        try:
            ids = [validate_id(value, "task") for value in dependencies]
        except TeamError:
            raise TeamError("team_task_dependency_invalid") from None
        if len(set(ids)) != len(ids):
            raise TeamError("team_task_dependency_invalid")

    @staticmethod
    def _has_dependents(view, task_id: str) -> bool:
        return any(not item.get("deleted", False) and task_id in item.get("dependencies", ())
                   for item in view.get("tasks", {}).values())

    @staticmethod
    def _ensure_dependencies_complete(view, task: TeamTask) -> None:
        tasks = view.get("tasks", {})
        if any(dep not in tasks or tasks[dep].get("deleted", False) for dep in task.dependencies):
            raise TeamError("team_task_dependency_invalid")
        if any(tasks[dep]["state"] != TaskState.COMPLETED.value for dep in task.dependencies):
            raise TeamError("team_task_not_ready")

    @staticmethod
    def _validate_dag(view, task_id: str, dependencies: tuple[str, ...]) -> None:
        """在 owner lock 内验证完整图，防止并发边更新形成环。"""
        tasks = view.get("tasks", {})
        for dependency in dependencies:
            if dependency == task_id:
                raise TeamError("team_task_dependency_invalid")
            record = tasks.get(dependency)
            if record is None or record.get("deleted", False):
                raise TeamError("team_task_dependency_invalid")

        graph = {
            key: tuple(value.get("dependencies", ()))
            for key, value in tasks.items() if not value.get("deleted", False) and key != task_id
        }
        graph[task_id] = tuple(dependencies)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise TeamError("team_task_dependency_cycle")
            if node in visited:
                return
            visiting.add(node)
            for child in graph.get(node, ()):
                if child not in graph:
                    raise TeamError("team_task_dependency_invalid")
                visit(child)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)
