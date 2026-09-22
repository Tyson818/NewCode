"""受控本地 Markdown 记忆存储与有限索引。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
from typing import Any

import yaml

from newcode.context.redaction import redact_text, redact_value
from newcode.persistence import workspace_fingerprint

from .types import MemoryCategory, MemoryNote, MemoryScope


DEFAULT_USER_MEMORY_ROOT = Path.home() / ".newcode" / "memory" / "user"
PROJECT_MEMORY_RELATIVE_ROOT = Path(".newcode") / "memory" / "project"
INDEX_NAME = "index.jsonl"
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25 * 1024
MAX_PROMPT_NOTES = 8
MAX_PROMPT_CHARACTERS = 6_000
_NOTE_ID = re.compile(r"^note-[a-z0-9]{16}$")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?im)\b((?=[a-z0-9_-]*(?:token|secret|password|credential|authorization|cookie|api[_-]?key))[a-z_][a-z0-9_-]*)\s*[:=]\s*[^\r\n]+"
)
_URL_CREDENTIAL = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")


class MemoryStoreError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MemoryStore:
    """user/project 两个 scope 的本地笔记和派生索引。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        user_root: Path | None = None,
        sensitive_values: Iterable[str] = (),
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=False)
        self.user_root = Path(user_root or DEFAULT_USER_MEMORY_ROOT)
        self.project_root = self.workspace_root / PROJECT_MEMORY_RELATIVE_ROOT
        self.sensitive_values = tuple(sensitive_values)
        self._id_factory = id_factory or _new_note_id

    def create(
        self,
        scope: MemoryScope,
        category: MemoryCategory,
        content: str,
        *,
        tags: Iterable[str] = (),
        now: datetime | None = None,
    ) -> MemoryNote:
        root = self._ensure_root(scope)
        timestamp = _utc(now)
        note = MemoryNote(
            id=self._new_available_id(root),
            category=category,
            scope=scope,
            content=redact_memory_text(_required_content(content), self.sensitive_values),
            tags=_safe_tags(tags, self.sensitive_values),
            created_at=timestamp,
            updated_at=timestamp,
            workspace_fingerprint=(workspace_fingerprint(self.workspace_root) if scope is MemoryScope.PROJECT else None),
        )
        notes = [*self._load_notes(scope), note]
        index = _index_text(_validate_index_notes(notes, scope, self.workspace_root))
        path = self._note_path(root, note.id)
        _atomic_write(path, _encode_note(note))
        try:
            _atomic_write(root / INDEX_NAME, index)
        except MemoryStoreError:
            path.unlink(missing_ok=True)
            raise
        return note

    def update(
        self,
        scope: MemoryScope,
        note_id: str,
        content: str,
        *,
        tags: Iterable[str] | None = None,
        category: MemoryCategory | None = None,
        now: datetime | None = None,
    ) -> MemoryNote:
        root = self._ensure_root(scope)
        path = self._note_path(root, note_id)
        original = _read_note(path, scope, self.workspace_root)
        if original is None:
            raise MemoryStoreError("memory_note_invalid")
        replacement = MemoryNote(
            id=original.id,
            category=category or original.category,
            scope=scope,
            content=redact_memory_text(_required_content(content), self.sensitive_values),
            tags=original.tags if tags is None else _safe_tags(tags, self.sensitive_values),
            created_at=original.created_at,
            updated_at=_utc(now),
            workspace_fingerprint=original.workspace_fingerprint,
        )
        notes = [note if note.id != note_id else replacement for note in self._load_notes(scope)]
        index = _index_text(_validate_index_notes(notes, scope, self.workspace_root))
        original_text = path.read_text(encoding="utf-8")
        _atomic_write(path, _encode_note(replacement))
        try:
            _atomic_write(root / INDEX_NAME, index)
        except MemoryStoreError:
            _atomic_write(path, original_text)
            raise
        return replacement

    def merge(
        self,
        scope: MemoryScope,
        note_id: str,
        additional_content: str,
        *,
        now: datetime | None = None,
    ) -> MemoryNote:
        root = self._ensure_root(scope)
        existing = _read_note(self._note_path(root, note_id), scope, self.workspace_root)
        if existing is None:
            raise MemoryStoreError("memory_note_invalid")
        return self.update(
            scope,
            note_id,
            f"{existing.content}\n\n{additional_content}",
            now=now,
        )

    def rebuild_index(self, scope: MemoryScope) -> None:
        root = self._ensure_root(scope)
        index = _index_text(_validate_index_notes(self._load_notes(scope), scope, self.workspace_root))
        _atomic_write(root / INDEX_NAME, index)

    def select_for_prompt(self) -> tuple[MemoryNote, ...]:
        notes = [*self._load_notes(MemoryScope.PROJECT), *self._load_notes(MemoryScope.USER)]
        notes.sort(key=lambda note: (-note.updated_at.timestamp(), note.scope.value, note.id))
        selected: list[MemoryNote] = []
        characters = 0
        for note in notes:
            rendered = _prompt_note(note, self.sensitive_values)
            if len(selected) >= MAX_PROMPT_NOTES or characters + len(rendered) > MAX_PROMPT_CHARACTERS:
                continue
            selected.append(note)
            characters += len(rendered)
        return tuple(selected)

    def prompt_background(self) -> str:
        notes = self.select_for_prompt()
        if not notes:
            return ""
        return "\n\n".join(_prompt_note(note, self.sensitive_values) for note in notes)

    def _load_notes(self, scope: MemoryScope) -> list[MemoryNote]:
        root = self._safe_existing_root(scope)
        if root is None:
            return []
        notes: list[MemoryNote] = []
        try:
            paths = tuple(root.glob("note-*.md"))
        except OSError:
            return []
        for path in paths:
            note = _read_note(path, scope, self.workspace_root)
            if note is not None:
                notes.append(note)
        return notes

    def _ensure_root(self, scope: MemoryScope) -> Path:
        root = self._root(scope)
        if root.is_symlink():
            raise MemoryStoreError("memory_index_failed")
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MemoryStoreError("memory_index_failed") from exc
        safe_root = self._safe_existing_root(scope)
        if safe_root is None:
            raise MemoryStoreError("memory_index_failed")
        return safe_root

    def _safe_existing_root(self, scope: MemoryScope) -> Path | None:
        root = self._root(scope)
        if not root.exists() or root.is_symlink() or not root.is_dir():
            return None
        resolved = root.resolve(strict=False)
        if scope is MemoryScope.PROJECT:
            try:
                resolved.relative_to(self.workspace_root)
            except ValueError:
                return None
        return resolved

    def _root(self, scope: MemoryScope) -> Path:
        return self.user_root if scope is MemoryScope.USER else self.project_root

    def _note_path(self, root: Path, note_id: str) -> Path:
        if not _NOTE_ID.fullmatch(note_id):
            raise MemoryStoreError("memory_note_invalid")
        path = root / f"{note_id}.md"
        if path.parent != root or path.is_symlink():
            raise MemoryStoreError("memory_note_invalid")
        return path

    def _new_available_id(self, root: Path) -> str:
        for _ in range(16):
            note_id = self._id_factory()
            if _NOTE_ID.fullmatch(note_id) and not self._note_path(root, note_id).exists():
                return note_id
        raise MemoryStoreError("memory_note_invalid")


def _new_note_id() -> str:
    return f"note-{secrets.token_hex(8)}"


def _required_content(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise MemoryStoreError("memory_note_invalid")
    return content.strip()


def _safe_tags(tags: Iterable[str], sensitive_values: tuple[str, ...]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for tag in tags:
        if isinstance(tag, str) and tag.strip():
            safe = redact_memory_text(tag.strip(), sensitive_values)
            if safe not in cleaned:
                cleaned.append(safe)
    return tuple(cleaned)


def _encode_note(note: MemoryNote) -> str:
    frontmatter: dict[str, Any] = {
        "version": 1,
        "id": note.id,
        "category": note.category.value,
        "scope": note.scope.value,
        "workspace_fingerprint": note.workspace_fingerprint,
        "created_at": note.created_at.isoformat(),
        "updated_at": note.updated_at.isoformat(),
        "tags": list(note.tags),
    }
    return f"---\n{yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=True)}---\n\n{note.content}\n"


def _read_note(path: Path, scope: MemoryScope, workspace: Path) -> MemoryNote | None:
    if path.is_symlink() or not path.is_file() or not _NOTE_ID.fullmatch(path.stem):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if not text.startswith("---\n"):
        return None
    closing = text.find("\n---\n", 4)
    if closing < 0:
        return None
    try:
        metadata = yaml.safe_load(text[4:closing])
    except yaml.YAMLError:
        return None
    if not isinstance(metadata, dict):
        return None
    try:
        category = MemoryCategory(metadata["category"])
        stored_scope = MemoryScope(metadata["scope"])
        created = _parse_time(metadata["created_at"])
        updated = _parse_time(metadata["updated_at"])
        tags = metadata.get("tags", [])
        note_id = metadata["id"]
    except (KeyError, TypeError, ValueError):
        return None
    if stored_scope is not scope or not _NOTE_ID.fullmatch(note_id) or not isinstance(tags, list) or created is None or updated is None:
        return None
    fingerprint = metadata.get("workspace_fingerprint")
    if scope is MemoryScope.PROJECT:
        if fingerprint != workspace_fingerprint(workspace):
            return None
    elif fingerprint is not None:
        return None
    content = text[closing + len("\n---\n") :].strip()
    if not content:
        return None
    return MemoryNote(note_id, category, scope, content, tuple(tag for tag in tags if isinstance(tag, str)), created, updated, fingerprint)


def _validate_index_notes(notes: list[MemoryNote], scope: MemoryScope, workspace: Path) -> list[dict[str, Any]]:
    records = [_index_record(note, workspace) for note in sorted(notes, key=lambda note: (note.updated_at, note.id), reverse=True)]
    if any(note.scope is not scope for note in notes) or len(records) > MAX_INDEX_LINES:
        raise MemoryStoreError("memory_index_failed")
    encoded = _index_text(records)
    if len(encoded.encode("utf-8")) > MAX_INDEX_BYTES:
        raise MemoryStoreError("memory_index_failed")
    return records


def _index_record(note: MemoryNote, workspace: Path) -> dict[str, Any]:
    return {
        "id": note.id,
        "category": note.category.value,
        "updated_at": note.updated_at.isoformat(),
        "tags": list(note.tags),
    }


def _index_text(records: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for record in records
    )


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as file:
            file.write(content)
        temporary.replace(path)
    except OSError as exc:
        raise MemoryStoreError("memory_index_failed") from exc
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink(missing_ok=True)


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _utc(parsed)


def _prompt_note(note: MemoryNote, sensitive_values: tuple[str, ...] = ()) -> str:
    return (
        f"【受控记忆｜来源：本地 MemoryStore｜scope：{note.scope.value}｜类别：{note.category.value}】\n"
        f"{redact_memory_text(note.content, sensitive_values)}"
    )


def redact_memory_text(value: str, sensitive_values: tuple[str, ...] = ()) -> str:
    redacted = redact_text(value, sensitive_values)
    redacted = _URL_CREDENTIAL.sub(r"\1[REDACTED]@", redacted)
    return _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}: [REDACTED]", redacted)
