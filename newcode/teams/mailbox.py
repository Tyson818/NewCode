"""持久邮箱、广播、读取与 checkpoint 证明后的确认。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Callable

from .store import TeamStore, encode, sanitize
from .types import (
    MAX_MESSAGE_BYTES, MAX_MESSAGES, MAX_PENDING_MESSAGES, RecordChange,
    MessageKind, TeamAck, TeamError, TeamMessage, TeamRole, new_id,
    validate_name, utc_now, thaw,
)


@dataclass(frozen=True)
class MailboxPage:
    messages: tuple[TeamMessage, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class MailboxWrite:
    messages: tuple[TeamMessage, ...]
    wake_failed: bool = False


@dataclass(frozen=True)
class DeliveryBatch:
    processed_message_ids: tuple[str, ...]
    session_id: str | None


class MailboxService:
    """实例绑定 runtime sender；模型接口中没有 sender/team/kind 字段。"""

    def __init__(self, store: TeamStore, team_id: str, actor_member_id: str, *,
                 clock: Callable[[], str] = utc_now,
                 wake: Callable[[str], None] | None = None,
                 cursor_key: bytes | None = None) -> None:
        self.store, self.team_id = store, team_id
        self.actor_member_id = validate_name(actor_member_id)
        self.clock = clock
        self.wake = wake or (lambda _member: None)
        self._cursor_key = cursor_key or secrets.token_bytes(32)

    def send(self, recipient: str, body: str, idempotency_key: str) -> MailboxWrite:
        if not isinstance(recipient, str) or validate_name(recipient) != recipient:
            raise TeamError("team_recipient_invalid")
        safe_body = self._body(body)
        key = self._scoped_key("send", idempotency_key)
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, transactions, _ = self.store._recover_locked(self.team_id)
            self._require_actor(view)
            if recipient not in view.get("members", {}):
                raise TeamError("team_recipient_invalid")
            replay = self._replay_intent(transactions, key, ((recipient, safe_body),), self.actor_member_id)
            if replay is not None:
                messages = replay
            else:
                self._check_capacity(view)
                sequence = transactions[-1].message_high_watermark + 1 if transactions else 1
                message = TeamMessage(new_id("message"), self.team_id, self.actor_member_id, recipient,
                                      safe_body, self.clock(), sequence, idempotency_key)
                self.store._commit_locked(self.team_id, transactions[-1].revision if transactions else 0,
                                          new_id("mutation"),
                                          (RecordChange("messages", message.message_id, message.to_dict()),), key)
                messages = (message,)
        return self._notify(messages, (recipient,))

    def broadcast(self, body: str, idempotency_key: str) -> MailboxWrite:
        safe_body = self._body(body)
        key = self._scoped_key("broadcast", idempotency_key)
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, transactions, _ = self.store._recover_locked(self.team_id)
            self._require_actor(view)
            roster = tuple(item["member_id"] for item in view["team"][self.team_id]["roster"]
                           if item["member_id"] != self.actor_member_id)
            if not roster:
                return MailboxWrite(())
            intent = tuple((recipient, safe_body) for recipient in roster)
            replay = self._replay_intent(transactions, key, intent, self.actor_member_id)
            if replay is not None:
                messages = replay
            else:
                self._check_capacity(view, count=len(roster))
                broadcast_id = new_id("broadcast")
                high = transactions[-1].message_high_watermark if transactions else 0
                messages = tuple(
                    TeamMessage(new_id("message"), self.team_id, self.actor_member_id, recipient,
                                safe_body, self.clock(), high + offset, idempotency_key,
                                broadcast_id=broadcast_id)
                    for offset, recipient in enumerate(roster, 1)
                )
                changes = tuple(RecordChange("messages", item.message_id, item.to_dict()) for item in messages)
                self.store._commit_locked(self.team_id, transactions[-1].revision if transactions else 0,
                                          new_id("mutation"), changes, key)
        return self._notify(messages, tuple(message.recipient for message in messages))

    def read(self, *, limit: int = 20, cursor: str | None = None) -> MailboxPage:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise TeamError("team_arguments_invalid")
        after = self._decode_cursor(cursor, limit) if cursor is not None else 0
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, _, _ = self.store._recover_locked(self.team_id)
            self._require_actor(view)
            acknowledged = set(view.get("acks", {}).get(self.actor_member_id, {}).get("acknowledged_message_ids", ()))
            inbox = [TeamMessage.from_dict(value) for value in view.get("messages", {}).values()
                     if value["recipient"] == self.actor_member_id and value["message_id"] not in acknowledged
                     and value["sequence"] > after]
            inbox.sort(key=lambda message: (message.sequence, message.message_id))
            selected, size = [], 0
            for message in inbox:
                estimate = len(message.body.encode("utf-8"))
                if selected and size + estimate > MAX_MESSAGE_BYTES:
                    break
                selected.append(message)
                size += estimate
                if len(selected) >= limit:
                    break
            has_more = len(selected) < len(inbox)
            next_cursor = self._encode_cursor(selected[-1].sequence, limit) if has_more and selected else None
            return MailboxPage(tuple(selected), next_cursor)

    def ack(self, message_id: str, *, session_id: str, checkpoints) -> bool:
        """有效 checkpoint 与 ack 提交在同一 Team lock 临界区内复核。"""
        from .member import MemberCheckpoints
        if not isinstance(checkpoints, MemberCheckpoints):
            raise TeamError("team_ack_checkpoint_required")
        if (checkpoints.store is not self.store or checkpoints.team_id != self.team_id
                or checkpoints.member_id != self.actor_member_id):
            raise TeamError("team_ack_checkpoint_required")
        with self.store.paths.guard(self.team_id), self.store.lock(self.team_id):
            view, transactions, _ = self.store._recover_locked(self.team_id)
            self._require_actor(view)
            envelope = view.get("messages", {}).get(message_id)
            if envelope is None:
                raise TeamError("team_message_not_found")
            if envelope["recipient"] != self.actor_member_id:
                raise TeamError("team_message_not_owned")
            try:
                restored = checkpoints._restore_locked(session_id, datetime.now(timezone.utc))
            except (TeamError, OSError, ValueError, TypeError):
                raise TeamError("team_ack_checkpoint_required") from None
            proof = restored.proof
            if (restored.session is None or proof is None or proof.team_id != self.team_id
                    or proof.member_id != self.actor_member_id or proof.session_id != session_id
                    or proof.generation != checkpoints.generation or message_id not in proof.processed_message_ids):
                raise TeamError("team_ack_checkpoint_required")
            current = view.get("acks", {}).get(self.actor_member_id)
            old = TeamAck.from_dict(current) if current else TeamAck(self.actor_member_id)
            if message_id in old.acknowledged_message_ids:
                return True
            ack = TeamAck(self.actor_member_id, (*old.acknowledged_message_ids, message_id), old.revision + (1 if current else 0))
            key = f"ack:{self.team_id}:{self.actor_member_id}:{message_id}"
            try:
                self.store._commit_locked(self.team_id, transactions[-1].revision if transactions else 0,
                                          new_id("mutation"),
                                          (RecordChange("acks", self.actor_member_id, ack.to_dict()),), key)
            except TeamError as exc:
                if exc.code != "team_idempotency_conflict":
                    raise
                # 同一消息的已提交确认可能在调用返回前崩溃；按日志重读结果。
                recovered, _, _ = self.store._recover_locked(self.team_id)
                if message_id not in recovered.get("acks", {}).get(self.actor_member_id, {}).get("acknowledged_message_ids", ()):
                    raise
            return True

    def deliver_pending(self, session, checkpoints) -> DeliveryBatch:
        """先把新 envelope 写入成员 session checkpoint，再逐条 ack。

        若 checkpoint 已发布而 ack 尚未提交，重试会从 proof 识别 message ID，
        不会再次向会话追加；若 checkpoint 未发布，邮件仍保持未确认状态。
        """
        from .member import MemberCheckpoints, delivery_message
        from newcode.session import ChatSession
        if (not isinstance(session, ChatSession) or not isinstance(checkpoints, MemberCheckpoints)
                or checkpoints.store is not self.store or checkpoints.team_id != self.team_id
                or checkpoints.member_id != self.actor_member_id):
            raise TeamError("team_ack_checkpoint_required")

        processed: set[str] = set()
        if session.session_id is not None:
            prior = checkpoints.restore(session.session_id)
            if prior.proof is not None:
                processed.update(prior.proof.processed_message_ids)

        cursor = None
        pending: list[TeamMessage] = []
        while True:
            page = self.read(limit=50, cursor=cursor)
            pending.extend(page.messages)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        for message in pending:
            if message.message_id not in processed:
                envelope_message = delivery_message(message.to_dict())
                if envelope_message not in session.messages:
                    session.messages.append(envelope_message)
                processed.add(message.message_id)

        # 即使当前没有新邮件，也重新发布本 generation 的 checkpoint，供恢复后 ack 使用。
        proof = checkpoints.checkpoint(session, processed_message_ids=tuple(sorted(processed)))
        for message in pending:
            if message.message_id not in proof.processed_message_ids:
                raise TeamError("team_ack_checkpoint_required")
            self.ack(message.message_id, session_id=proof.session_id, checkpoints=checkpoints)
        return DeliveryBatch(tuple(sorted(processed)), proof.session_id)

    def _require_actor(self, view):
        members = view.get("members", {})
        if self.actor_member_id not in members:
            raise TeamError("team_scope_invalid")
        return members[self.actor_member_id]

    def _body(self, body: str) -> str:
        if not isinstance(body, str):
            raise TeamError("team_message_invalid")
        safe = sanitize(body, self.store.sensitive_values)
        if not isinstance(safe, str) or not safe.strip():
            raise TeamError("team_message_invalid")
        if len(safe.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise TeamError("team_mailbox_full")
        return safe

    def _scoped_key(self, operation: str, key: str) -> str:
        if not isinstance(key, str) or not __import__("re").fullmatch(r"[A-Za-z0-9_-]{1,64}", key):
            raise TeamError("team_arguments_invalid")
        return f"{self.team_id}:{self.actor_member_id}:{operation}:{key}"

    def _check_capacity(self, view, *, count=1):
        messages = view.get("messages", {})
        if len(messages) + count > MAX_MESSAGES:
            raise TeamError("team_mailbox_full")
        acknowledged = set()
        for record in view.get("acks", {}).values():
            acknowledged.update(record.get("acknowledged_message_ids", ()))
        pending = len(set(messages) - acknowledged)
        if pending + count > MAX_PENDING_MESSAGES:
            raise TeamError("team_mailbox_full")

    @staticmethod
    def _replay_intent(transactions, idempotency_key, intent, sender):
        for transaction in transactions:
            if transaction.idempotency_key != idempotency_key:
                continue
            messages = tuple(TeamMessage.from_dict(thaw(change.value))
                             for change in transaction.changes if change.collection == "messages")
            actual = tuple((message.recipient, message.body) for message in messages)
            if (not messages or any(message.sender_member_id != sender or message.kind is not MessageKind.MESSAGE
                                    for message in messages)
                    or actual != intent):
                raise TeamError("team_idempotency_conflict")
            return messages
        return None

    def _notify(self, messages, recipients):
        failed = False
        for recipient in recipients:
            try:
                self.wake(recipient)
            except Exception:
                failed = True
        return MailboxWrite(tuple(messages), failed)

    def _encode_cursor(self, sequence: int, limit: int) -> str:
        payload = encode({"team": self.team_id, "member": self.actor_member_id,
                          "sequence": sequence, "limit": limit,
                          "expires": int(time.time()) + 600})
        signature = hmac.new(self._cursor_key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")

    def _decode_cursor(self, cursor: str, limit: int) -> int:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload, signature = raw[:-32], raw[-32:]
            expected = hmac.new(self._cursor_key, payload, hashlib.sha256).digest()
            value = json.loads(payload)
            if (not hmac.compare_digest(signature, expected) or value.get("team") != self.team_id
                    or value.get("member") != self.actor_member_id or type(value.get("sequence")) is not int
                    or value["sequence"] < 0 or value.get("limit") != limit
                    or type(value.get("expires")) is not int or value["expires"] < int(time.time())):
                raise ValueError
            return value["sequence"]
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise TeamError("team_cursor_invalid") from None


def make_system_message(team_id: str, sender: str, recipient: str, kind: MessageKind,
                        payload: dict, sequence: int, *, now: str | None = None) -> TeamMessage:
    """供任务/审批可信服务创建固定协议 envelope；模型 send API 不暴露此入口。"""
    if kind not in {MessageKind.TASK_ASSIGNED, MessageKind.TASK_COMPLETED,
                    MessageKind.PLAN_SUBMITTED, MessageKind.PLAN_DECISION,
                    MessageKind.MEMBER_IDLE, MessageKind.MEMBER_FAILED}:
        raise TeamError("team_message_invalid")
    safe_payload = sanitize(payload)
    body = encode({"protocol": kind.value, "data": safe_payload}).decode("utf-8")
    return TeamMessage(new_id("message"), team_id, sender, recipient, body,
                       now or utc_now(), sequence, f"event-{secrets.token_hex(8)}",
                       kind=kind, payload=safe_payload)
