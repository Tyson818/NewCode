from __future__ import annotations

import os

import pytest

from newcode.tools.file_tools import ReadFileTool, ReplaceInFileTool, WriteFileTool
from newcode.tools.search_tools import FindFilesTool, SearchCodeTool
from newcode.tools.types import ToolContext


def test_tool_context_has_fixed_canonical_root_cwd_and_identity(tmp_path):
    root = tmp_path / "worktree"
    root.mkdir()
    cwd = root / "nested"
    cwd.mkdir()
    context = ToolContext(root, cwd=cwd)

    assert context.workspace_root == root.resolve()
    assert context.cwd == cwd.resolve()
    assert context.workspace_identity
    with pytest.raises((AttributeError, TypeError)):
        context.cwd = root  # type: ignore[misc]
    with pytest.raises(ValueError, match="tool_context_cwd_outside_workspace"):
        ToolContext(root, cwd=tmp_path)


def test_file_and_search_tools_are_bound_to_child_root(tmp_path):
    main = tmp_path / "main"
    child = tmp_path / "child"
    main.mkdir()
    child.mkdir()
    (main / "same.txt").write_text("main-only", encoding="utf-8")
    (child / "same.txt").write_text("child-only", encoding="utf-8")
    context = ToolContext(child, worktree_task_id="task-1234")

    assert ReadFileTool().run({"path": "same.txt"}, context).data["content"] == "child-only"
    assert ReadFileTool().run({"path": str(main / "same.txt")}, context).error.code == "path_not_allowed"
    assert WriteFileTool().run({"path": "new.txt", "content": "child"}, context).ok
    assert not (main / "new.txt").exists()
    assert ReplaceInFileTool().run(
        {"path": "same.txt", "old_text": "child-only", "new_text": "changed"}, context,
    ).ok
    assert (main / "same.txt").read_text(encoding="utf-8") == "main-only"
    assert FindFilesTool().run({"pattern": "*.txt"}, context).data["matches"] == ["new.txt", "same.txt"]
    assert SearchCodeTool().run({"query": "changed", "pattern": "*.txt"}, context).data["matches"][0]["path"] == "same.txt"


def test_worktree_git_pointer_and_shared_git_metadata_are_not_file_tool_roots(tmp_path):
    main = tmp_path / "main"
    child = main / ".newcode" / "worktrees" / "agent" / "task-1234"
    shared_git = main / ".git"
    child.mkdir(parents=True)
    shared_git.mkdir()
    (shared_git / "config").write_text("shared metadata", encoding="utf-8")
    (child / ".git").write_text("gitdir: ../../../../.git/worktrees/task-1234", encoding="utf-8")
    context = ToolContext(child, worktree_task_id="task-1234")

    pointer = ReadFileTool().run({"path": ".git"}, context)
    metadata = ReadFileTool().run({"path": str(shared_git / "config")}, context)
    assert not pointer.ok and pointer.error.code == "path_not_allowed"
    assert not metadata.ok and metadata.error.code == "path_not_allowed"
    assert (shared_git / "config").read_text(encoding="utf-8") == "shared metadata"


def test_find_and_search_skip_symlink_escape(tmp_path):
    root = tmp_path / "child"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_text("do not expose", encoding="utf-8")
    try:
        os.symlink(outside, root / "escape.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"符号链接 fixture 不可用: {exc}")

    context = ToolContext(root, worktree_task_id="task-1234")
    assert FindFilesTool().run({"pattern": "*.txt"}, context).data["matches"] == []
    assert SearchCodeTool().run({"query": "do not expose"}, context).data["matches"] == []
