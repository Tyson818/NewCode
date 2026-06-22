from newcode.tools.search_tools import FindFilesTool, SearchCodeTool
from newcode.tools.types import ToolContext


def context(tmp_path):
    return ToolContext(workspace_root=tmp_path)


def test_find_files_returns_matches(tmp_path):
    (tmp_path / "a.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "b.txt").write_text("text", encoding="utf-8")

    result = FindFilesTool().run({"pattern": "*.py"}, context(tmp_path))

    assert result.ok is True
    assert result.data["matches"] == ["a.py"]


def test_find_files_returns_empty_list(tmp_path):
    result = FindFilesTool().run({"pattern": "*.missing"}, context(tmp_path))

    assert result.ok is True
    assert result.data["matches"] == []


def test_find_files_skips_ignored_dirs(tmp_path):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "ignored.py").write_text("x", encoding="utf-8")

    result = FindFilesTool().run({"pattern": "*.py"}, context(tmp_path))

    assert result.data["matches"] == []


def test_search_code_returns_file_line_and_text(tmp_path):
    (tmp_path / "a.py").write_text("alpha\nProviderError here\n", encoding="utf-8")

    result = SearchCodeTool().run(
        {"query": "ProviderError", "pattern": "*.py"},
        context(tmp_path),
    )

    assert result.ok is True
    assert result.data["matches"] == [
        {"path": "a.py", "line": 2, "text": "ProviderError here"}
    ]


def test_search_code_limits_results(tmp_path):
    (tmp_path / "a.py").write_text("hit\nhit\n", encoding="utf-8")

    result = SearchCodeTool().run(
        {"query": "hit", "pattern": "*.py", "max_results": 1},
        context(tmp_path),
    )

    assert len(result.data["matches"]) == 1
    assert result.data["truncated"] is True
