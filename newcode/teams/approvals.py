"""结构化计划修订与审批；审批不产生 Permission grant。"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from .store import TeamStore
from .types import (
    MemberState, MessageKind, PlanState, RecordChange, TeamError,
    TeamMemberRecord, TeamPlan, TeamRole, TeamTask, new_id, utc_now,
    validate_transition,
)


class ApprovalService:
    """单个 Team runtime principal 的审批端口。

    actor 和 mode 由可信 runtime 注入，调用参数中没有 actor/decision-maker
    字段。mode 只代表 AgentMode 的受控快照；PLAN 下所有业务写拒绝。
    """

    def __init__(self, store: TeamStore, team_id: str, actor_member_id: str, *,
                 mode: str = "do", clock: Callable[[], str] = utc_now) -> None:
        if mode not in {"plan", "do"}:
            raise TeamError("team_arguments_invalid")
        self.store, self.team_id, self.actor_member_id = store, team_id, actor_member_id
        self.mode, self.clock = mode, clock

    def submit(self, task_id: str, content: str, *, plan_id: str | None = None,
               expected_plan_revision: int | None = None) -> TeamPlan:
        self._require_do()
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, transactions, _ = self.store._recover_locked(self.team_id)
            member = self._actor(view, TeamRole.MEMBER)
            if member.state not in {MemberState.RUNNING, MemberState.AWAITING_APPROVAL}:
                raise TeamError("team_state_invalid")
            task_value = view.get("tasks", {}).get(task_id)
            if task_value is None or task_value.get("deleted", False):
                raise TeamError("team_task_not_found")
            task = TeamTask.from_dict(task_value)
            if task.assignee != self.actor_member_id or task.state.value in {"completed", "cancelled"}:
                raise TeamError("team_role_denied")

            plans = view.get("plans", {})
            prior = None
            if plan_id is None:
                if expected_plan_revision is not None:
                    raise TeamError("team_arguments_invalid")
                plan_id = new_id("plan")
                revision = 1
            else:
                value = plans.get(plan_id)
                if value is None:
                    raise TeamError("team_plan_not_found")
                prior = TeamPlan.from_dict(value)
                if prior.member_id != self.actor_member_id or prior.task_id != task_id:
                    raise TeamError("team_plan_not_found")
                if type(expected_plan_revision) is not int or expected_plan_revision != prior.revision:
                    raise TeamError("team_plan_stale")
                if prior.state is PlanState.SUPERSEDED:
                    raise TeamError("team_plan_stale")
                revision = prior.revision + 1

            now = self.clock()
            high = transactions[-1].message_high_watermark if transactions else 0
            notice = self._system_event(self.actor_member_id, "lead", MessageKind.PLAN_SUBMITTED,
                                        {"plan_id": plan_id, "task_id": task_id, "revision": revision},
                                        high + 1, now)
            plan = TeamPlan(plan_id, self.actor_member_id, task_id, content, now, revision,
                            PlanState.PENDING, message_id=notice.message_id)
            member_changes = []
            if member.plan_revision != revision or member.state is MemberState.RUNNING:
                state = MemberState.AWAITING_APPROVAL if member.state is MemberState.RUNNING else member.state
                if state is not member.state:
                    validate_transition(member.state, state)
                updated_member = replace(member, plan_revision=revision, state=state,
                                         revision=member.revision + 1)
                member_changes.append(RecordChange("members", member.member_id, updated_member.to_dict()))
            changes = (RecordChange("plans", plan_id, plan.to_dict()),
                       *member_changes,
                       RecordChange("messages", notice.message_id, notice.to_dict()))
            self._commit(transactions, changes)
            return plan

    def approve(self, plan_id: str, expected_plan_revision: int, *, reason: str = "") -> TeamPlan:
        return self._decide(plan_id, expected_plan_revision, approved=True, reason=reason)

    def reject(self, plan_id: str, expected_plan_revision: int, *, reason: str) -> TeamPlan:
        return self._decide(plan_id, expected_plan_revision, approved=False, reason=reason)

    def _decide(self, plan_id, expected_plan_revision, *, approved, reason):
        self._require_do()
        self._validate_reason(reason, required=not approved)
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, transactions, _ = self.store._recover_locked(self.team_id)
            lead = self._actor(view, TeamRole.LEAD)
            value = view.get("plans", {}).get(plan_id)
            if value is None:
                raise TeamError("team_plan_not_found")
            plan = TeamPlan.from_dict(value)
            if type(expected_plan_revision) is not int or expected_plan_revision != plan.revision or plan.state is not PlanState.PENDING:
                raise TeamError("team_plan_stale")
            member_value = view.get("members", {}).get(plan.member_id)
            if member_value is None:
                raise TeamError("team_member_not_found")
            member = TeamMemberRecord.from_dict(member_value)
            if member.state is MemberState.INTEGRATED:
                raise TeamError("team_plan_stale")

            now = self.clock()
            high = transactions[-1].message_high_watermark if transactions else 0
            target_state = PlanState.APPROVED if approved else PlanState.REJECTED
            event = self._system_event(lead.member_id, plan.member_id, MessageKind.PLAN_DECISION,
                                       {"plan_id": plan.plan_id, "revision": plan.revision,
                                        "decision": target_state.value, "reason": reason},
                                       high + 1, now)
            updated = replace(plan, state=target_state, decision_member_id=lead.member_id,
                              decision_at=now, message_id=event.message_id)
            changes = [RecordChange("plans", plan.plan_id, updated.to_dict()),
                       RecordChange("messages", event.message_id, event.to_dict())]
            if approved and member.state is MemberState.AWAITING_APPROVAL:
                validate_transition(member.state, MemberState.RUNNING)
                resumed = replace(member, state=MemberState.RUNNING, revision=member.revision + 1)
                changes.append(RecordChange("members", member.member_id, resumed.to_dict()))
            self._commit(transactions, tuple(changes))
            return updated

    def _actor(self, view, expected_role):
        value = view.get("members", {}).get(self.actor_member_id)
        if value is None:
            raise TeamError("team_scope_invalid")
        member = TeamMemberRecord.from_dict(value)
        if member.role is not expected_role:
            raise TeamError("team_role_denied")
        return member

    def _require_do(self):
        if self.mode != "do":
            raise TeamError("disallowed_tool")

    @staticmethod
    def _validate_reason(reason, *, required):
        if not isinstance(reason, str) or (required and not reason.strip()) or len(reason) > 2000:
            raise TeamError("team_arguments_invalid")
        if any(ord(char) < 32 and char not in "\n\t" for char in reason):
            raise TeamError("team_arguments_invalid")

    def _commit(self, transactions, changes):
        expected = transactions[-1].revision if transactions else 0
        try:
            self.store._commit_locked(self.team_id, expected, new_id("mutation"), changes, None)
        except TeamError as exc:
            if exc.code == "team_revision_conflict":
                raise TeamError("team_plan_stale") from None
            raise

    def _system_event(self, sender, recipient, kind, payload, sequence, now):
        from .mailbox import make_system_message
        return make_system_message(self.team_id, sender, recipient, kind, payload, sequence, now=now)
