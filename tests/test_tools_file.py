from newcode.tools.file_tools import ReadFileTool, ReplaceInFileTool, WriteFileTool
from newcode.tools.types import ToolContext


def context(tmp_path):
    return ToolContext(workspace_root=tmp_path)


def test_read_file_success(tmp_path):
    (tmp_path / "hello.txt").write_text("你好", encoding="utf-8")

    result = ReadFileTool().run({"path": "hello.txt"}, context(tmp_path))

    assert result.ok is True
    assert result.data == {"path": "hello.txt", "content": "你好"}


def test_read_file_missing_returns_failure(tmp_path):
    result = ReadFileTool().run({"path": "missing.txt"}, context(tmp_path))

    assert result.ok is False
    assert result.error.code == "file_not_found"


def test_read_file_outside_workspace_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    result = ReadFileTool().run({"path": str(outside)}, context(tmp_path))

    assert result.ok is False
    assert result.error.code == "path_not_allowed"


def test_write_file_creates_parent_directories(tmp_path):
    result = WriteFileTool().run(
        {"path": "nested/file.txt", "content": "内容"},
        context(tmp_path),
    )

    assert result.ok is True
    assert (tmp_path / "nested" / "file.txt").read_text(encoding="utf-8") == "内容"


def test_replace_in_file_replaces_unique_match(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("a old b", encoding="utf-8")

    result = ReplaceInFileTool().run(
        {"path": "sample.txt", "old_text": "old", "new_text": "new"},
        context(tmp_path),
    )

    assert result.ok is True
    assert path.read_text(encoding="utf-8") == "a new b"


def test_replace_in_file_zero_match_does_not_modify(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("a old b", encoding="utf-8")

    result = ReplaceInFileTool().run(
        {"path": "sample.txt", "old_text": "missing", "new_text": "new"},
        context(tmp_path),
    )

    assert result.ok is False
    assert result.error.code == "not_unique_match"
    assert path.read_text(encoding="utf-8") == "a old b"


def test_replace_in_file_multiple_matches_do_not_modify(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("old old", encoding="utf-8")

    result = ReplaceInFileTool().run(
        {"path": "sample.txt", "old_text": "old", "new_text": "new"},
        context(tmp_path),
    )

    assert result.ok is False
    assert result.error.code == "not_unique_match"
    assert result.error.details["matches"] == 2
    assert path.read_text(encoding="utf-8") == "old old"
