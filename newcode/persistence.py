"""本地 JSONL 会话归档与安全恢复。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
from typing import Any

from newcode.context.redaction import redact_text, redact_value
from newcode.session import ChatMessage, ChatSession
from newcode.tools.types import ToolCall


DEFAULT_SESSIONS_ROOT = Path.home() / ".newcode" / "sessions"
SESSION_FORMAT_VERSION = 1
SESSION_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[a-z0-9]{4}$")
MAX_CREATE_ATTEMPTS = 16
STALE_SESSION_AGE = timedelta(days=30)
TIME_SPAN_REMINDER_AGE = timedelta(hours=24)
_SUFFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


@dataclass(frozen=True)
class SessionRestoreResult:
    session: ChatSession | None
    errors: tuple[str, ...] = ()
    truncated: bool = False
    needs_time_span_reminder: bool = False


@dataclass(frozen=True)
class SessionCleanupResult:
    removed_session_ids: tuple[str, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    title: str
    updated_at: datetime
    message_count: int


class SessionArchive:
    """只管理一个受控 sessions 根目录内的普通 JSONL 文件。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        sessions_root: Path | None = None,
        sensitive_values: Iterable[str] = (),
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=False)
        self.sessions_root = Path(sessions_root or DEFAULT_SESSIONS_ROOT)
        self.sensitive_values = tuple(sensitive_values)

    def create(
        self,
        session: ChatSession,
        *,
        now: datetime | None = None,
        suffix_factory: Callable[[], str] | None = None,
    ) -> str:
        """以排他方式创建新归档，冲突只重抽四位随机后缀。"""

        created = _as_utc(now)
        suffix = suffix_factory or _random_suffix
        root = self._ensure_root()
        prefix = created.strftime("%Y%m%d-%H%M%S")
        for _ in range(MAX_CREATE_ATTEMPTS):
            session_id = f"{prefix}-{suffix()}"
            if not is_valid_session_id(session_id):
                continue
            path = self._path_for_id(session_id, root)
            try:
                with path.open("x", encoding="utf-8", newline="\n") as file:
                    file.write(_encode_line(self._metadata(session_id, created)))
                session.session_id = session_id
                return session_id
            except FileExistsError:
                continue
            except OSError as exc:
                raise SessionArchiveError("session_archive_failed") from exc
        raise SessionArchiveError("session_archive_failed")

    def checkpoint(
        self,
        session: ChatSession,
        *,
        now: datetime | None = None,
    ) -> None:
        """安全重写本会话快照；仅接受此前创建过的合法 ID。"""

        if not session.session_id or not is_valid_session_id(session.session_id):
            raise SessionArchiveError("session_archive_failed")
        updated = _as_utc(now)
        root = self._ensure_root()
        path = self._path_for_id(session.session_id, root)
        if not _is_safe_regular_file(path, root):
            raise SessionArchiveError("session_archive_failed")

        records = [self._metadata(session.session_id, updated)]
        records.extend(_message_record(message, self.sensitive_values) for message in session.messages)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            if temporary.exists() or temporary.is_symlink():
                raise SessionArchiveError("session_archive_failed")
            with temporary.open("x", encoding="utf-8", newline="\n") as file:
                for record in records:
                    file.write(_encode_line(record))
            if not _is_safe_regular_file(path, root):
                raise SessionArchiveError("session_archive_failed")
            temporary.replace(path)
        except SessionArchiveError:
            raise
        except OSError as exc:
            raise SessionArchiveError("session_archive_failed") from exc
        finally:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink(missing_ok=True)

    def restore(
        self,
        session_id: str,
        *,
        now: datetime | None = None,
    ) -> SessionRestoreResult:
        """恢复可验证消息，跳过坏行，并截断第一个异常工具交换。"""

        if not is_valid_session_id(session_id):
            return SessionRestoreResult(None, ("session_restore_failed",))
        root = self._safe_existing_root()
        if root is None:
            return SessionRestoreResult(None, ("session_restore_failed",))
        path = self._path_for_id(session_id, root)
        if not _is_safe_regular_file(path, root):
            return SessionRestoreResult(None, ("session_restore_failed",))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return SessionRestoreResult(None, ("session_restore_failed",))
        if not lines:
            return SessionRestoreResult(None, ("session_restore_failed",))

        metadata = _parse_metadata(lines[0], session_id, self.workspace_root)
        if metadata is None:
            return SessionRestoreResult(None, ("session_restore_failed",))
        updated = _parse_time(metadata.get("updated_at"))
        if updated is None or _as_utc(now) - updated > STALE_SESSION_AGE:
            return SessionRestoreResult(None, ("session_restore_failed",))
        messages: list[ChatMessage] = []
        errors: list[str] = []
        for line in lines[1:]:
            message = _parse_message_record(line)
            if message is None:
                _add_code(errors, "session_record_invalid")
                continue
            messages.append(message)
        retained, truncated = _truncate_incomplete_tool_exchange(messages)
        if truncated:
            _add_code(errors, "session_tool_pair_truncated")
        reminder = updated is not None and _as_utc(now) - updated >= TIME_SPAN_REMINDER_AGE
        return SessionRestoreResult(
            session=ChatSession(messages=retained, session_id=session_id),
            errors=tuple(errors),
            truncated=truncated,
            needs_time_span_reminder=reminder,
        )

    def cleanup_stale(
        self,
        *,
        active_session_id: str | None = None,
        now: datetime | None = None,
    ) -> SessionCleanupResult:
        """仅删除根目录内、有效且过期的普通归档文件。"""

        root = self._safe_existing_root()
        if root is None:
            return SessionCleanupResult(())
        current = _as_utc(now)
        removed: list[str] = []
        errors: list[str] = []
        try:
            entries = tuple(root.iterdir())
        except OSError:
            return SessionCleanupResult((), ("session_archive_failed",))
        for entry in entries:
            session_id = entry.stem
            if (
                session_id == active_session_id
                or entry.suffix != ".jsonl"
                or not is_valid_session_id(session_id)
                or not _is_safe_regular_file(entry, root)
            ):
                continue
            try:
                with entry.open(encoding="utf-8") as file:
                    first_line = file.readline()
            except (OSError, UnicodeError):
                continue
            metadata = _parse_metadata(first_line, session_id, self.workspace_root)
            updated = _parse_time(metadata.get("updated_at")) if metadata else None
            if updated is None or current - updated <= STALE_SESSION_AGE:
                continue
            try:
                entry.unlink()
                removed.append(session_id)
            except OSError:
                _add_code(errors, "session_archive_failed")
        return SessionCleanupResult(tuple(removed), tuple(errors))

    def list_recoverable(self, *, now: datetime | None = None) -> tuple[SessionSummary, ...]:
        """只返回当前 workspace 内未过期、可恢复归档的安全摘要。"""

        root = self._safe_existing_root()
        if root is None:
            return ()
        current = _as_utc(now)
        summaries: list[SessionSummary] = []
        try:
            entries = tuple(root.iterdir())
        except OSError:
            return ()
        for entry in entries:
            session_id = entry.stem
            if entry.suffix != ".jsonl" or not is_valid_session_id(session_id) or not _is_safe_regular_file(entry, root):
                continue
            try:
                with entry.open(encoding="utf-8") as file:
                    lines = file.read().splitlines()
            except (OSError, UnicodeError):
                continue
            if not lines:
                continue
            metadata = _parse_metadata(lines[0], session_id, self.workspace_root)
            updated = _parse_time(metadata.get("updated_at")) if metadata else None
            if updated is None or current - updated > STALE_SESSION_AGE:
                continue
            count = sum(_parse_message_record(line) is not None for line in lines[1:])
            summaries.append(SessionSummary(session_id, "已归档会话", updated, count))
        return tuple(sorted(summaries, key=lambda item: (item.updated_at, item.session_id), reverse=True))

    def _metadata(self, session_id: str, updated: datetime) -> dict[str, str | int]:
        timestamp = updated.isoformat()
        return {
            "record_type": "metadata",
            "version": SESSION_FORMAT_VERSION,
            "session_id": session_id,
            "workspace_fingerprint": workspace_fingerprint(self.workspace_root),
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def _ensure_root(self) -> Path:
        if self.sessions_root.is_symlink():
            raise SessionArchiveError("session_archive_failed")
        try:
            self.sessions_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionArchiveError("session_archive_failed") from exc
        root = self._safe_existing_root()
        if root is None:
            raise SessionArchiveError("session_archive_failed")
        return root

    def _safe_existing_root(self) -> Path | None:
        if not self.sessions_root.exists() or self.sessions_root.is_symlink():
            return None
        root = self.sessions_root.resolve(strict=False)
        return root if root.is_dir() else None

    def _path_for_id(self, session_id: str, root: Path) -> Path:
        if not is_valid_session_id(session_id):
            raise SessionArchiveError("session_archive_failed")
        path = root / f"{session_id}.jsonl"
        if path.parent != root:
            raise SessionArchiveError("session_archive_failed")
        return path


class SessionArchiveError(Exception):
    """对外只暴露安全错误码，避免归档细节泄露。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_valid_session_id(value: str) -> bool:
    return bool(SESSION_ID_PATTERN.fullmatch(value))


def workspace_fingerprint(workspace_root: Path) -> str:
    return hashlib.sha256(str(Path(workspace_root).resolve(strict=False)).encode("utf-8")).hexdigest()


def _random_suffix() -> str:
    return "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(4))


def _message_record(message: ChatMessage, sensitive_values: tuple[str, ...]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": message.role,
        "content": _redact_message_content(message, sensitive_values),
        "tool_call_id": message.tool_call_id,
    }
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "name": call.name,
                "arguments": _json_safe(redact_value(call.arguments, sensitive_values), sensitive_values),
                "raw_arguments": redact_text(call.raw_arguments, sensitive_values),
            }
            for call in message.tool_calls
        ]
    return {"record_type": "message", "message": payload}


def _redact_message_content(message: ChatMessage, sensitive_values: tuple[str, ...]) -> str | None:
    if message.content is None:
        return None
    if message.role != "tool":
        return redact_text(message.content, sensitive_values)
    try:
        decoded = json.loads(message.content)
    except json.JSONDecodeError:
        return redact_text(message.content, sensitive_values)
    return json.dumps(
        _json_safe(redact_value(decoded, sensitive_values), sensitive_values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_safe(value: Any, sensitive_values: tuple[str, ...]) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_json_safe(item, sensitive_values) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item, sensitive_values) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item, sensitive_values)
            for key, item in value.items()
        }
    return redact_text(str(value), sensitive_values)


def _encode_line(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"


def _parse_metadata(line: str, session_id: str, workspace: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    if (
        value.get("record_type") != "metadata"
        or value.get("version") != SESSION_FORMAT_VERSION
        or value.get("session_id") != session_id
        or value.get("workspace_fingerprint") != workspace_fingerprint(workspace)
        or _parse_time(value.get("updated_at")) is None
    ):
        return None
    return value


def _parse_message_record(line: str) -> ChatMessage | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or record.get("record_type") != "message":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    content = message.get("content")
    tool_call_id = message.get("tool_call_id")
    if role not in ("system", "user", "assistant", "tool") or content is not None and not isinstance(content, str):
        return None
    if tool_call_id is not None and not isinstance(tool_call_id, str):
        return None
    raw_calls = message.get("tool_calls")
    calls = None if raw_calls is None else _parse_tool_calls(raw_calls)
    if raw_calls is not None and calls is None:
        return None
    try:
        return ChatMessage(role=role, content=content, tool_calls=calls, tool_call_id=tool_call_id)
    except (TypeError, ValueError):
        return None


def _parse_tool_calls(value: Any) -> list[ToolCall] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        return None
    calls: list[ToolCall] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        call_id = item.get("id")
        name = item.get("name")
        arguments = item.get("arguments")
        raw_arguments = item.get("raw_arguments")
        if (
            not isinstance(call_id, str)
            or not isinstance(name, str)
            or not isinstance(arguments, dict)
            or not isinstance(raw_arguments, str)
        ):
            return None
        calls.append(ToolCall(call_id, name, arguments, raw_arguments))
    return calls


def _truncate_incomplete_tool_exchange(messages: list[ChatMessage]) -> tuple[list[ChatMessage], bool]:
    pending: set[str] = set()
    exchange_start: int | None = None
    for index, message in enumerate(messages):
        if pending:
            if message.role != "tool" or message.tool_call_id not in pending:
                return messages[: exchange_start or 0], True
            pending.remove(message.tool_call_id)
            if not pending:
                exchange_start = None
            continue
        if message.role == "tool":
            return messages[:index], True
        if message.tool_calls:
            call_ids = [call.id for call in message.tool_calls]
            if len(call_ids) != len(set(call_ids)) or any(not call_id for call_id in call_ids):
                return messages[:index], True
            pending = set(call_ids)
            exchange_start = index
    if pending:
        return messages[: exchange_start or 0], True
    return messages, False


def _is_safe_regular_file(path: Path, root: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return path.resolve(strict=False).parent == root.resolve(strict=False)
    except OSError:
        return False


def _as_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _as_utc(parsed)


def _add_code(codes: list[str], code: str) -> None:
    if code not in codes:
        codes.append(code)
