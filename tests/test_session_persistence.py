from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from newcode.persistence import (
    MAX_CREATE_ATTEMPTS,
    SessionArchive,
    SessionArchiveError,
    is_valid_session_id,
)
from newcode.session import ChatMessage, ChatSession
from newcode.tools.types import ToolCall, ToolResult


NOW = datetime(2026, 9, 22, 8, 30, 15, tzinfo=timezone.utc)


def _archive(tmp_path: Path, *, workspace: Path | None = None) -> SessionArchive:
    actual_workspace = workspace or tmp_path / "workspace"
    actual_workspace.mkdir(exist_ok=True)
    return SessionArchive(actual_workspace, sessions_root=tmp_path / "home" / ".newcode" / "sessions", sensitive_values=("super-secret",))


def _session_with_tool_exchange() -> ChatSession:
    session = ChatSession()
    session.add_user_message("read file with super-secret")
    call = ToolCall("call-1", "read_file", {"path": "a.py", "token": "super-secret"}, '{"token":"super-secret"}')
    session.add_assistant_tool_call(call)
    session.add_tool_result("call-1", ToolResult.success("read_file", {"authorization": "super-secret", "content": "ok"}))
    session.add_assistant_message("done")
    return session


def test_create_uses_fixed_session_id_format_and_checkpoint_is_redacted(tmp_path: Path):
    archive = _archive(tmp_path)
    session = _session_with_tool_exchange()

    session_id = archive.create(session, now=NOW, suffix_factory=lambda: "a1z9")
    archive.checkpoint(session, now=NOW)

    assert session_id == "20260922-083015-a1z9"
    assert is_valid_session_id(session_id)
    content = (archive.sessions_root / f"{session_id}.jsonl").read_text(encoding="utf-8")
    assert content.splitlines()[0]
    assert '"record_type": "metadata"' in content
    assert "super-secret" not in content
    assert "[REDACTED]" in content


def test_create_retries_only_suffix_and_never_overwrites_existing_archive(tmp_path: Path):
    archive = _archive(tmp_path)
    first = ChatSession()
    first_id = archive.create(first, now=NOW, suffix_factory=lambda: "aaaa")
    first_path = archive.sessions_root / f"{first_id}.jsonl"
    first_path.write_text("original", encoding="utf-8")
    suffixes = iter(("aaaa", "bbbb"))

    second_id = archive.create(ChatSession(), now=NOW, suffix_factory=lambda: next(suffixes))

    assert second_id == "20260922-083015-bbbb"
    assert first_path.read_text(encoding="utf-8") == "original"


def test_create_fails_safely_after_collision_limit(tmp_path: Path):
    archive = _archive(tmp_path)
    archive.sessions_root.mkdir(parents=True)
    session_id = "20260922-083015-aaaa"
    (archive.sessions_root / f"{session_id}.jsonl").write_text("keep", encoding="utf-8")

    with pytest.raises(SessionArchiveError, match="session_archive_failed"):
        archive.create(ChatSession(), now=NOW, suffix_factory=lambda: "aaaa")

    assert (archive.sessions_root / f"{session_id}.jsonl").read_text(encoding="utf-8") == "keep"
    assert MAX_CREATE_ATTEMPTS == 16


def test_restore_skips_bad_and_unknown_records_but_keeps_valid_messages(tmp_path: Path):
    archive = _archive(tmp_path)
    session = ChatSession()
    session.add_user_message("kept")
    session_id = archive.create(session, now=NOW, suffix_factory=lambda: "a1z9")
    archive.checkpoint(session, now=NOW)
    path = archive.sessions_root / f"{session_id}.jsonl"
    with path.open("a", encoding="utf-8") as file:
        file.write("not-json\n")
        file.write(json.dumps({"record_type": "unknown"}) + "\n")
        file.write(json.dumps({"record_type": "message", "message": {"role": "bad"}}) + "\n")

    restored = archive.restore(session_id, now=NOW)

    assert restored.session is not None
    assert [message.content for message in restored.session.messages] == ["kept"]
    assert restored.errors == ("session_record_invalid",)


@pytest.mark.parametrize(
    "messages, expected_contents",
    [
        ([ChatMessage("assistant", None, [ToolCall("call-1", "read_file")]), ChatMessage("user", "after")], []),
        ([ChatMessage("tool", "{}", tool_call_id="orphan"), ChatMessage("user", "after")], []),
        (
            [
                ChatMessage("user", "before"),
                ChatMessage("assistant", None, [ToolCall("call-1", "read_file"), ToolCall("call-2", "read_file")]),
                ChatMessage("tool", "{}", tool_call_id="call-1"),
                ChatMessage("user", "interrupt"),
            ],
            ["before"],
        ),
    ],
)
def test_restore_truncates_at_first_unpaired_tool_exchange(tmp_path: Path, messages, expected_contents):
    archive = _archive(tmp_path)
    session = ChatSession(messages=messages)
    session_id = archive.create(session, now=NOW, suffix_factory=lambda: "a1z9")
    archive.checkpoint(session, now=NOW)

    restored = archive.restore(session_id, now=NOW)

    assert restored.session is not None
    assert [message.content for message in restored.session.messages] == expected_contents
    assert restored.truncated is True
    assert "session_tool_pair_truncated" in restored.errors


def test_restore_marks_sessions_older_than_twenty_four_hours(tmp_path: Path):
    archive = _archive(tmp_path)
    session = ChatSession()
    session_id = archive.create(session, now=NOW, suffix_factory=lambda: "a1z9")

    restored = archive.restore(session_id, now=NOW + timedelta(hours=24))

    assert restored.session is not None
    assert restored.needs_time_span_reminder is True


def test_restore_rejects_cross_workspace_without_exposing_archive(tmp_path: Path):
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    archive_a = _archive(tmp_path, workspace=workspace_a)
    session_id = archive_a.create(ChatSession(), now=NOW, suffix_factory=lambda: "a1z9")
    archive_b = _archive(tmp_path, workspace=workspace_b)

    restored = archive_b.restore(session_id, now=NOW)

    assert restored.session is None
    assert restored.errors == ("session_restore_failed",)


def test_cleanup_removes_only_valid_stale_nonactive_regular_archives(tmp_path: Path):
    archive = _archive(tmp_path)
    stale = ChatSession()
    stale_id = archive.create(stale, now=NOW - timedelta(days=31), suffix_factory=lambda: "aaaa")
    active = ChatSession()
    active_id = archive.create(active, now=NOW - timedelta(days=31), suffix_factory=lambda: "bbbb")
    bad = archive.sessions_root / "20260922-083015-cccc.jsonl"
    bad.write_text("not a metadata record", encoding="utf-8")
    directory = archive.sessions_root / "20260922-083015-dddd.jsonl"
    directory.mkdir()

    result = archive.cleanup_stale(active_session_id=active_id, now=NOW)

    assert result.removed_session_ids == (stale_id,)
    assert not (archive.sessions_root / f"{stale_id}.jsonl").exists()
    assert (archive.sessions_root / f"{active_id}.jsonl").exists()
    assert bad.exists()
    assert directory.exists()


def test_cleanup_skips_symbolic_links(tmp_path: Path):
    archive = _archive(tmp_path)
    archive.sessions_root.mkdir(parents=True)
    target = tmp_path / "outside.jsonl"
    target.write_text("keep", encoding="utf-8")
    link = archive.sessions_root / "20260922-083015-a1z9.jsonl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("当前平台不允许测试符号链接")

    archive.cleanup_stale(now=NOW + timedelta(days=31))

    assert link.exists()
    assert target.read_text(encoding="utf-8") == "keep"
