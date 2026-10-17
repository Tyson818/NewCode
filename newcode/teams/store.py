"""带跨进程锁、CAS 与可重放单提交记录的 TeamStore 基础。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any

from newcode.context.redaction import redact_text

from .paths import TeamPaths, durable_publish, verify_private
from .types import (
    Backend, MAX_JOURNAL_BYTES, MAX_MESSAGES, MAX_PENDING_MESSAGES, MAX_TASKS, MAX_TEAMS, MAX_TRANSACTIONS,
    MAX_TRANSACTION_BYTES, RecordChange, TeamAck, TeamError, TeamManifest, TeamMemberRecord,
    TeamRole, TeamTask, TeamPlan, TeamTransaction, freeze, new_id, positive, thaw, utc_now, validate_id,
    validate_transition, IntegrationRecord,
)

_ASSIGNMENT = re.compile(r'''(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie|(?:[a-z0-9_-]*(?:password|secret|credential|api[_-]?key|access_token|refresh_token)))\s*["']?\s*[:=]\s*[^\r\n]+''')
_URL = re.compile(r"(?i)(https?://)[^\s/@]+@")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"',}]+")


def sanitize(value: Any, sensitive_values: tuple[str, ...] = ()) -> Any:
    """只处理 JSON 值；永远不序列化任意运行对象的 repr。"""
    value = thaw(freeze(value))
    def clean(item):
        if isinstance(item, str):
            if item.lstrip().startswith(("{", "[")):
                try:
                    decoded = json.loads(item)
                except (ValueError, RecursionError):
                    decoded = None
                if isinstance(decoded, (dict, list)):
                    return json.dumps(clean(thaw(freeze(decoded))), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            result = redact_text(item, sensitive_values)
            result = _URL.sub(r"\1[REDACTED]@", result)
            result = _BEARER.sub("Bearer [REDACTED]", result)
            return _ASSIGNMENT.sub("[REDACTED]", result)
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                normalized = key.lower().replace("-", "_")
                private = normalized in {"headers", "env", "environment", "authorization", "proxy_authorization", "cookie", "password", "secret", "token", "api_key", "apikey", "credentials"}
                result[key] = "[REDACTED]" if private else clean(child)
            return result
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item
    return clean(value)


def encode(value: Any) -> bytes:
    try:
        return json.dumps(thaw(freeze(value)), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise TeamError("team_store_corrupt") from None


def _decode(data: bytes) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError
            value[key] = item
        return value
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise TeamError("team_store_corrupt") from None


class TeamFileLock:
    """内核锁是唯一互斥依据；owner 元数据不是 stale-lock 定时器。"""

    def __init__(self, paths: TeamPaths, parts: tuple[str, ...], *, scope: str, timeout: float = 1.0) -> None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 <= timeout <= 1:
            raise TeamError("team_arguments_invalid")
        self.paths, self.parts, self.scope, self.timeout = paths, parts, scope, timeout
        self.fd = None
        self._locked = False
        self._metadata_owned = False
        self._path_guard = None

    def __enter__(self):
        try:
            self._path_guard = self.paths.guard(*self.parts[:-1])
            self._path_guard.__enter__()
            self.paths.mkdir(*self.parts[:-1])
            path = self.paths.checked(*self.parts, missing=True)
            self.fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self.paths.checked(*self.parts)
            info = os.fstat(self.fd)
            if not os.path.samestat(info, path.stat()) or info.st_nlink != 1:
                raise TeamError("team_path_invalid")
            if info.st_size == 0:
                os.write(self.fd, b"\0")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                    elif os.name == "posix":
                        import fcntl
                        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        raise TeamError("team_store_unavailable")
                    self._locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TeamError("team_store_busy") from None
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            self.paths.checked(*self.parts)
            os.lseek(self.fd, 1, os.SEEK_SET)
            previous = os.read(self.fd, 2049)
            if previous:
                try:
                    owner = _decode(previous)
                    if set(owner) != {"pid", "nonce", "scope"} or type(owner["pid"]) is not int or owner["pid"] <= 0 or owner["scope"] != self.scope or not re.fullmatch(r"[0-9a-f]{32}", owner["nonce"]):
                        raise ValueError
                except (TeamError, TypeError, ValueError):
                    raise TeamError("team_store_busy") from None
            owner = encode({"pid": os.getpid(), "nonce": secrets.token_hex(16), "scope": self.scope})
            os.lseek(self.fd, 1, os.SEEK_SET)
            os.write(self.fd, owner)
            os.ftruncate(self.fd, len(owner) + 1)
            os.fsync(self.fd)
            self._metadata_owned = True
            return self
        except OSError:
            self.__exit__(None, None, None)
            raise TeamError("team_store_unavailable") from None
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.fd is None:
            if self._path_guard is not None:
                self._path_guard.__exit__(None, None, None)
                self._path_guard = None
            return
        try:
            if self._locked:
                if self._metadata_owned:
                    os.ftruncate(self.fd, 1)
                    os.fsync(self.fd)
                os.lseek(self.fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            raise TeamError("team_store_unavailable") from None
        finally:
            os.close(self.fd)
            self.fd = None
            if self._path_guard is not None:
                self._path_guard.__exit__(None, None, None)
                self._path_guard = None


@dataclass(frozen=True)
class CommitResult:
    transaction_id: str
    revision: int
    commit_sequence: int
    replayed: bool = False


class TeamStore:
    def __init__(self, paths: TeamPaths, *, sensitive_values: Iterable[str] = (), fault: Callable[[str], None] | None = None) -> None:
        self.paths = paths
        self.sensitive_values = tuple(sensitive_values)
        self._fault = fault or (lambda _: None)

    def lock(self, team_id: str, *, timeout: float = 1.0) -> TeamFileLock:
        self.paths.team(team_id)
        return TeamFileLock(self.paths, (team_id, "locks", "store.lock"), scope=team_id, timeout=timeout)

    def atomic_write(self, parts: tuple[str, ...], data: bytes, *, replace: bool = True) -> None:
        """内部固定路径端口；完整临时文件后才发布，不截断旧目标。"""
        temporary_parts = (*parts[:-1], f".{secrets.token_hex(16)}.tmp")
        with self.paths.guard(*parts[:-1]):
            target = self.paths.checked(*parts, missing=True)
            temporary = self.paths.checked(*temporary_parts, missing=True)
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                with os.fdopen(fd, "wb") as stream:
                    self.paths.checked(*temporary_parts)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.paths.checked(*parts, missing=True)
                self.paths.checked(*temporary_parts)
                self._fault("before_publish")
                durable_publish(temporary, target, replace=replace)
            except OSError:
                raise TeamError("team_store_unavailable") from None
            finally:
                if temporary.exists():
                    self.paths.checked(*temporary_parts)
                    temporary.unlink()

    def read_bytes(self, parts: tuple[str, ...], *, maximum: int) -> bytes:
        with self.paths.guard(*parts[:-1]):
            path = self.paths.checked(*parts)
            if not path.is_file() or path.stat().st_size > maximum:
                raise TeamError("team_store_corrupt")
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as stream:
                    if not os.path.samestat(os.fstat(stream.fileno()), path.stat()):
                        raise TeamError("team_path_invalid")
                    data = stream.read(maximum + 1)
                self.paths.checked(*parts)
                if len(data) > maximum:
                    raise TeamError("team_store_corrupt")
                return data
            except OSError:
                raise TeamError("team_store_unavailable") from None

    def create_team(self, name: str, *, backend: Backend = Backend.LIGHTWEIGHT) -> TeamManifest:
        timestamp = utc_now()
        lead = TeamMemberRecord("lead", "lead", role=TeamRole.LEAD)
        # 校验先于任何创建，避免非法名称留下目录。
        prototype = TeamManifest(new_id("team"), name, self.paths.workspace_fingerprint, "lead", (lead,), timestamp, timestamp, backend=backend)
        self.paths.ensure_root()
        with self.paths.guard(), TeamFileLock(self.paths, ("_locks", "create.lock"), scope="create"):
            teams = [p for p in self.paths.root.iterdir() if p.name.startswith("team-")]
            if len(teams) >= MAX_TEAMS:
                raise TeamError("team_limit_exceeded")
            for _ in range(16):
                team_id = prototype.team_id
                if not self.paths.team(team_id, missing=True).exists():
                    break
                prototype = replace(prototype, team_id=new_id("team"))
            else:
                raise TeamError("team_name_conflict")
            self.paths.mkdir(team_id)
            self.paths.mkdir(team_id, "transactions")
            self.paths.mkdir(team_id, "locks")
            with self.paths.guard(team_id), self.lock(team_id):
                changes = (RecordChange("team", team_id, prototype.to_dict()), RecordChange("members", "lead", lead.to_dict()))
                self._commit_locked(team_id, 0, new_id("mutation"), changes, None)
        return self.load_team(team_id)

    def commit_transaction(self, team_id: str, *, expected_revision: int, mutation_id: str,
                           changes: Iterable[RecordChange], idempotency_key: str | None = None) -> CommitResult:
        positive(expected_revision)
        validate_id(mutation_id, "mutation")
        frozen_changes = tuple(changes)
        if not frozen_changes or any(not isinstance(c, RecordChange) for c in frozen_changes):
            raise TeamError("team_arguments_invalid")
        with self.paths.guard(team_id), self.lock(team_id):
            return self._commit_locked(team_id, expected_revision, mutation_id, frozen_changes, idempotency_key)

    def load_team(self, team_id: str) -> TeamManifest:
        with self.paths.guard(team_id), self.lock(team_id):
            view, _, _ = self._recover_locked(team_id)
            try:
                return TeamManifest.from_dict(view["team"][team_id])
            except KeyError:
                raise TeamError("team_store_corrupt") from None

    def load_records(self, team_id: str, collection: str) -> tuple[dict, ...]:
        if collection not in {"members", "tasks", "messages", "plans", "acks", "integrations"}:
            raise TeamError("team_arguments_invalid")
        with self.paths.guard(team_id), self.lock(team_id):
            view, _, _ = self._recover_locked(team_id)
            return tuple(view.get(collection, {}).values())

    def _commit_locked(self, team_id, expected, mutation_id, changes, idempotency_key):
        changes = tuple(RecordChange(c.collection, c.record_id, sanitize(c.value, self.sensitive_values)) for c in changes)
        if any(c.collection == "team" and c.record_id != team_id for c in changes):
            raise TeamError("team_scope_invalid")
        request_digest = hashlib.sha256(encode([c.to_dict() for c in changes])).hexdigest()
        view, transactions, journal_bytes = self._recover_locked(team_id, allow_empty=expected == 0)
        for transaction in transactions:
            if transaction.mutation_id == mutation_id or idempotency_key is not None and transaction.idempotency_key == idempotency_key:
                if transaction.request_digest != request_digest:
                    raise TeamError("team_idempotency_conflict")
                return CommitResult(transaction.transaction_id, transaction.revision, transaction.commit_sequence, True)
        actual = transactions[-1].revision if transactions else 0
        if actual != expected:
            raise TeamError("team_revision_conflict")
        if len({(c.collection, c.record_id) for c in changes}) != len(changes):
            raise TeamError("team_arguments_invalid")
        old_manifest = view.get("team", {}).get(team_id)
        explicit = next((c for c in changes if c.collection == "team"), None)
        if explicit:
            manifest = TeamManifest.from_dict(thaw(explicit.value))
            if manifest.team_id != team_id or manifest.revision != expected + 1:
                raise TeamError("team_revision_conflict")
        elif old_manifest:
            manifest = replace(TeamManifest.from_dict(old_manifest), revision=expected + 1, updated_at=utc_now())
        else:
            raise TeamError("team_store_corrupt")
        if manifest.workspace_fingerprint != self.paths.workspace_fingerprint:
            raise TeamError("team_workspace_mismatch")
        roster = {m.member_id: m for m in manifest.roster}
        for change in changes:
            old = view.get(change.collection, {}).get(change.record_id)
            if old and "revision" in old and change.collection != "team":
                valid_plan_version = (change.collection == "plans" and
                                      change.value["revision"] in {old["revision"], old["revision"] + 1})
                if not valid_plan_version and change.value["revision"] != old["revision"] + 1:
                    raise TeamError("team_revision_conflict")
            if old and "state" in old and change.value["state"] != old["state"]:
                old_record = RecordChange(change.collection, change.record_id, old)
                cls = {"team": TeamManifest, "members": TeamMemberRecord}.get(change.collection)
                if cls:
                    validate_transition(cls.from_dict(thaw(old_record.value)).state, cls.from_dict(thaw(change.value)).state)
            if change.collection == "members":
                roster[change.record_id] = TeamMemberRecord.from_dict(thaw(change.value))
        manifest = replace(manifest, roster=tuple(roster.values()))
        combined = tuple(c for c in changes if c.collection != "team") + (RecordChange("team", team_id, manifest.to_dict()),)
        high = transactions[-1].message_high_watermark if transactions else 0
        for change in combined:
            if change.collection == "messages":
                if change.record_id in view.get("messages", {}) or change.value["sequence"] != high + 1:
                    raise TeamError("team_revision_conflict")
                if change.value["team_id"] != team_id or change.value["recipient"] not in roster or change.value["sender_member_id"] not in roster:
                    raise TeamError("team_scope_invalid")
                high += 1
        transaction = TeamTransaction(new_id("transaction"), team_id, len(transactions) + 1,
                                      transactions[-1].transaction_id if transactions else None,
                                      expected, expected + 1, mutation_id, request_digest, combined, high, idempotency_key)
        candidate = self._validated_apply(view, transaction, transactions[-1] if transactions else None)
        payload = transaction.to_dict()
        payload["digest"] = hashlib.sha256(encode(payload)).hexdigest()
        data = encode(payload)
        if len(data) > MAX_TRANSACTION_BYTES or len(transactions) >= MAX_TRANSACTIONS or journal_bytes + len(data) > MAX_JOURNAL_BYTES:
            raise TeamError("team_limit_exceeded")
        filename = f"{transaction.commit_sequence:020d}-{transaction.transaction_id}.json"
        self._fault("before_commit")
        self.atomic_write((team_id, "transactions", filename), data, replace=False)
        self._fault("after_commit")
        self._project(team_id, candidate)
        return CommitResult(transaction.transaction_id, transaction.revision, transaction.commit_sequence)

    def _recover_locked(self, team_id, *, allow_empty=False):
        root = self.paths.checked(team_id, "transactions")
        files = []
        for path in root.iterdir():
            if re.fullmatch(r"\.[0-9a-f]{32}\.tmp", path.name):
                self.paths.checked(team_id, "transactions", path.name)
                continue
            if not re.fullmatch(r"[0-9]{20}-transaction-[0-9a-f]{32}\.json", path.name):
                raise TeamError("team_store_corrupt")
            files.append(path)
        if len(files) > MAX_TRANSACTIONS or not files and not allow_empty:
            raise TeamError("team_store_corrupt")
        transactions, view, size, ids, keys = [], {}, 0, set(), set()
        for path in sorted(files):
            data = self.read_bytes((team_id, "transactions", path.name), maximum=MAX_TRANSACTION_BYTES)
            size += len(data)
            if size > MAX_JOURNAL_BYTES:
                raise TeamError("team_store_corrupt")
            payload = _decode(data)
            digest = payload.pop("digest", None)
            if digest != hashlib.sha256(encode(payload)).hexdigest():
                raise TeamError("team_store_corrupt")
            try:
                tx = TeamTransaction.from_dict(payload)
            except TeamError:
                raise TeamError("team_store_corrupt") from None
            prior = transactions[-1] if transactions else None
            if tx.team_id != team_id or tx.commit_sequence != len(transactions) + 1 or tx.expected_revision != (prior.revision if prior else 0) or tx.previous_commit_id != (prior.transaction_id if prior else None) or path.name != f"{tx.commit_sequence:020d}-{tx.transaction_id}.json":
                raise TeamError("team_store_corrupt")
            if tx.mutation_id in ids or tx.idempotency_key is not None and tx.idempotency_key in keys:
                raise TeamError("team_store_corrupt")
            ids.add(tx.mutation_id)
            keys.add(tx.idempotency_key)
            try:
                view = self._validated_apply(view, tx, prior)
            except TeamError:
                raise TeamError("team_store_corrupt") from None
            manifest = view.get("team", {}).get(team_id)
            if not manifest or manifest["revision"] != tx.revision:
                raise TeamError("team_store_corrupt")
            if manifest["workspace_fingerprint"] != self.paths.workspace_fingerprint:
                raise TeamError("team_workspace_mismatch")
            transactions.append(tx)
        if transactions:
            self._project(team_id, view)
        return view, transactions, size

    @staticmethod
    def _validated_apply(view, transaction, prior):
        result = {name: dict(records) for name, records in view.items()}
        high = prior.message_high_watermark if prior else 0
        broadcasts = {}
        classes = {"team": TeamManifest, "members": TeamMemberRecord, "tasks": TeamTask,
                   "plans": TeamPlan, "acks": TeamAck}
        for change in transaction.changes:
            old = result.get(change.collection, {}).get(change.record_id)
            if change.collection == "integrations":
                record = IntegrationRecord.from_dict(thaw(change.value))
                if record.team_id != transaction.team_id:
                    raise TeamError("team_scope_invalid")
                if old:
                    IntegrationRecord.from_dict(old).validate_successor(record)
                elif record.revision != 1 or record.stage != "frozen":
                    raise TeamError("team_state_invalid")
            if old and "revision" in old:
                valid_plan_version = (change.collection == "plans" and
                                      change.value["revision"] in {old["revision"], old["revision"] + 1})
                if not valid_plan_version and change.value["revision"] != old["revision"] + 1:
                    raise TeamError("team_revision_conflict")
            if old and change.collection == "plans":
                prior_plan = TeamPlan.from_dict(old)
                next_plan = TeamPlan.from_dict(thaw(change.value))
                if (prior_plan.plan_id, prior_plan.member_id, prior_plan.task_id) != (
                        next_plan.plan_id, next_plan.member_id, next_plan.task_id):
                    raise TeamError("team_plan_stale")
                if next_plan.revision == prior_plan.revision:
                    if (next_plan.content, next_plan.submitted_at) != (prior_plan.content, prior_plan.submitted_at):
                        raise TeamError("team_plan_stale")
                elif next_plan.revision == prior_plan.revision + 1:
                    if (next_plan.state.value != "pending" or next_plan.decision_member_id is not None
                            or next_plan.decision_at is not None):
                        raise TeamError("team_plan_stale")
            if old and "state" in old and old["state"] != change.value["state"]:
                if (change.collection == "plans" and old["state"] in {"pending", "approved", "rejected"}
                        and change.value["state"] == "pending"
                        and change.value.get("revision") == old.get("revision", 0) + 1
                        and change.value.get("decision_member_id") is None
                        and change.value.get("decision_at") is None):
                    # 新计划 revision 重新进入 pending；旧审批只留在不可变事务历史中。
                    pass
                else:
                    cls = classes[change.collection]
                    validate_transition(cls.from_dict(old).state, cls.from_dict(thaw(change.value)).state)
            if change.collection == "messages":
                if old or change.value["sequence"] != high + 1:
                    raise TeamError("team_revision_conflict")
                high += 1
                if change.value["team_id"] != transaction.team_id:
                    raise TeamError("team_scope_invalid")
                if change.value["broadcast_id"]:
                    broadcasts.setdefault(change.value["broadcast_id"], []).append(change.value)
            if change.collection == "acks" and old:
                if not set(old["acknowledged_message_ids"]) <= set(change.value["acknowledged_message_ids"]):
                    raise TeamError("team_arguments_invalid")
            if change.collection == "team" and change.record_id != transaction.team_id:
                raise TeamError("team_scope_invalid")
            result.setdefault(change.collection, {})[change.record_id] = thaw(change.value)
        if high != transaction.message_high_watermark:
            raise TeamError("team_store_corrupt")
        manifest = result.get("team", {}).get(transaction.team_id)
        if not manifest:
            raise TeamError("team_store_corrupt")
        roster = {member["member_id"]: member for member in manifest["roster"]}
        if roster != result.get("members", {}):
            raise TeamError("team_store_corrupt")
        for message in result.get("messages", {}).values():
            if message["recipient"] not in roster or message["sender_member_id"] not in roster:
                raise TeamError("team_scope_invalid")
        messages = result.get("messages", {})
        integration_keys = set()
        for value in result.get("integrations", {}).values():
            record = IntegrationRecord.from_dict(value)
            key = record.member_id, record.idempotency_key
            if record.member_id not in roster or key in integration_keys:
                raise TeamError("team_idempotency_conflict")
            integration_keys.add(key)
            if any(c.collection == "integrations" and c.record_id == record.integration_id
                   for c in transaction.changes):
                member = roster[record.member_id]
                expected_revision = record.member_revision + (2 if record.stage == "completed" else 1)
                if (member["role"] != "member" or member["runtime_generation"] != record.generation
                        or member["base_commit"] != record.base_commit or member["revision"] != expected_revision
                        or member["auto_wake"] or (record.stage == "completed" and member["state"] != "integrated")):
                    raise TeamError("team_scope_invalid")
        acknowledged = set()
        for member_id, ack in result.get("acks", {}).items():
            if member_id not in roster or ack["member_id"] != member_id:
                raise TeamError("team_scope_invalid")
            acked = set(ack["acknowledged_message_ids"])
            if not acked <= set(messages):
                raise TeamError("team_message_not_found")
            if any(messages[message_id]["recipient"] != member_id for message_id in acked):
                raise TeamError("team_message_not_owned")
            acknowledged.update(acked)
        for envelopes in broadcasts.values():
            recipients = [m["recipient"] for m in envelopes]
            expected_recipients = [member_id for member_id in roster if member_id != envelopes[0]["sender_member_id"]]
            if recipients != expected_recipients or len(set(recipients)) != len(envelopes):
                raise TeamError("team_message_invalid")
            if len({(m["body"], m["sender_member_id"], m["kind"], m["idempotency_key"]) for m in envelopes}) != 1:
                raise TeamError("team_message_invalid")
        TeamStore._validate_task_graph(result.get("tasks", {}))
        pending = sum(1 for message_id in messages if message_id not in acknowledged)
        if len(result.get("tasks", {})) > MAX_TASKS or len(messages) > MAX_MESSAGES or pending > MAX_PENDING_MESSAGES:
            raise TeamError("team_limit_exceeded")
        return result

    @staticmethod
    def _validate_task_graph(tasks):
        graph = {task_id: tuple(record.get("dependencies", ()))
                 for task_id, record in tasks.items() if not record.get("deleted", False)}
        for task_id, dependencies in graph.items():
            if len(dependencies) > 16 or len(set(dependencies)) != len(dependencies):
                raise TeamError("team_task_dependency_invalid")
            for dependency in dependencies:
                if dependency == task_id or dependency not in graph:
                    raise TeamError("team_task_dependency_invalid")
        visiting, visited = set(), set()

        def visit(task_id):
            if task_id in visiting:
                raise TeamError("team_task_dependency_cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in graph[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in graph:
            visit(task_id)

    def _project(self, team_id, view):
        for collection, records in view.items():
            if collection != "team":
                self.paths.mkdir(team_id, collection)
            for record_id, value in records.items():
                parts = (team_id, "team.json") if collection == "team" else (team_id, collection, f"{record_id}.json")
                data = encode(value)
                path = self.paths.checked(*parts, missing=True)
                if path.exists() and path.stat().st_size <= MAX_TRANSACTION_BYTES and self.read_bytes(parts, maximum=MAX_TRANSACTION_BYTES) == data:
                    continue
                self.atomic_write(parts, data)
                self._fault(f"after_projection:{collection}")
