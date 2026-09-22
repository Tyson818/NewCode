from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from newcode.memory.store import (
    MAX_INDEX_BYTES,
    MAX_INDEX_LINES,
    MAX_PROMPT_CHARACTERS,
    MAX_PROMPT_NOTES,
    MemoryStore,
    MemoryStoreError,
)
from newcode.memory.types import MemoryCategory, MemoryScope


NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


def _store(tmp_path: Path, *, values: tuple[str, ...] = ()) -> MemoryStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return MemoryStore(workspace, user_root=tmp_path / "home" / ".newcode" / "memory" / "user", sensitive_values=values)


def test_creates_frontmatter_markdown_with_controlled_filename_and_redaction(tmp_path: Path):
    store = _store(tmp_path, values=("secret-value",))
    note = store.create(MemoryScope.USER, MemoryCategory.USER_PREFERENCE, "偏好 secret-value", tags=("token",), now=NOW)

    path = store.user_root / f"{note.id}.md"
    text = path.read_text(encoding="utf-8")
    assert path.name.startswith("note-")
    assert text.startswith("---\n") and "category: 用户偏好" in text
    assert "secret-value" not in text
    assert "[REDACTED]" in text


def test_redacts_headers_environment_assignments_and_url_credentials(tmp_path: Path):
    store = _store(tmp_path)
    note = store.create(
        MemoryScope.USER,
        MemoryCategory.REFERENCE,
        "Authorization: Bearer abc\nDEEPSEEK_API_KEY=xyz\nhttps://name:pass@example.test/path",
        now=NOW,
    )
    text = (store.user_root / f"{note.id}.md").read_text(encoding="utf-8")

    assert "Bearer abc" not in text
    assert "xyz" not in text
    assert "name:pass@" not in text
    assert "[REDACTED]" in text


def test_project_scope_requires_matching_workspace_fingerprint(tmp_path: Path):
    store = _store(tmp_path)
    note = store.create(MemoryScope.PROJECT, MemoryCategory.PROJECT_KNOWLEDGE, "项目规则", now=NOW)
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    other = MemoryStore(other_workspace, user_root=store.user_root)
    other.project_root = store.project_root

    assert store.select_for_prompt() == (note,)
    assert other.select_for_prompt() == ()


def test_index_enforces_both_line_and_byte_caps_for_create(tmp_path: Path):
    store = _store(tmp_path)
    for _ in range(MAX_INDEX_LINES):
        store.create(MemoryScope.USER, MemoryCategory.REFERENCE, "x", now=NOW)
    with pytest.raises(MemoryStoreError, match="memory_index_failed"):
        store.create(MemoryScope.USER, MemoryCategory.REFERENCE, "overflow", now=NOW)

    byte_store = _store(tmp_path / "bytes")
    huge_tags = ("x" * MAX_INDEX_BYTES,)
    with pytest.raises(MemoryStoreError, match="memory_index_failed"):
        byte_store.create(MemoryScope.USER, MemoryCategory.REFERENCE, "small", tags=huge_tags, now=NOW)
    assert not list(byte_store.user_root.glob("note-*.md"))


def test_update_merge_and_rebuild_validate_index_caps(tmp_path: Path):
    store = _store(tmp_path)
    note = store.create(MemoryScope.USER, MemoryCategory.CORRECTION_FEEDBACK, "old", now=NOW)
    with pytest.raises(MemoryStoreError, match="memory_index_failed"):
        store.update(MemoryScope.USER, note.id, "new", tags=("x" * MAX_INDEX_BYTES,), now=NOW)
    assert "old" in (store.user_root / f"{note.id}.md").read_text(encoding="utf-8")
    store.merge(MemoryScope.USER, note.id, "added", now=NOW)
    store.rebuild_index(MemoryScope.USER)
    assert (store.user_root / "index.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_merge_and_rebuild_reject_notes_that_exceed_index_byte_cap(tmp_path: Path):
    store = _store(tmp_path)
    note = store.create(MemoryScope.USER, MemoryCategory.REFERENCE, "known", now=NOW)
    path = store.user_root / f"{note.id}.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "tags: []",
            f"tags:\n- {'x' * MAX_INDEX_BYTES}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(MemoryStoreError, match="memory_index_failed"):
        store.merge(MemoryScope.USER, note.id, "more", now=NOW)
    with pytest.raises(MemoryStoreError, match="memory_index_failed"):
        store.rebuild_index(MemoryScope.USER)


def test_select_for_prompt_is_stable_and_bounded(tmp_path: Path):
    store = _store(tmp_path)
    for index in range(10):
        store.create(MemoryScope.USER, MemoryCategory.USER_PREFERENCE, f"note {index}" + "x" * 500, now=NOW)

    selected = store.select_for_prompt()
    rendered = store.prompt_background()

    assert len(selected) == MAX_PROMPT_NOTES
    assert len(rendered) <= MAX_PROMPT_CHARACTERS
    assert selected == store.select_for_prompt()


def test_invalid_or_symbolic_note_is_not_loaded(tmp_path: Path):
    store = _store(tmp_path)
    store.user_root.mkdir(parents=True)
    (store.user_root / "note-not-a-controlled-id.md").write_text("bad", encoding="utf-8")
    target = tmp_path / "target.md"
    target.write_text("outside", encoding="utf-8")
    link = store.user_root / "note-0123456789abcdef.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("当前平台不允许测试符号链接")

    assert store.select_for_prompt() == ()
