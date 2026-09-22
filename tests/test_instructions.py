from __future__ import annotations

from pathlib import Path

import pytest

from newcode.instructions import (
    InstructionSource,
    MAX_INCLUDE_DEPTH,
    load_project_instructions,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _contents(result) -> list[tuple[InstructionSource, str]]:
    return [(item.source, item.content) for item in result.instructions]


def test_loads_three_layers_and_returns_high_priority_first(tmp_path: Path):
    workspace = tmp_path / "workspace"
    user_root = tmp_path / "user" / ".newcode"
    workspace.mkdir()
    _write(user_root / "INSTRUCTIONS.md", "user rule")
    _write(workspace / ".newcode" / "INSTRUCTIONS.md", "workspace rule")
    _write(workspace / "AGENTS.md", "project rule")

    result = load_project_instructions(workspace, user_root=user_root)

    assert _contents(result) == [
        (InstructionSource.PROJECT, "project rule"),
        (InstructionSource.WORKSPACE, "workspace rule"),
        (InstructionSource.USER, "user rule"),
    ]
    assert result.errors == ()


def test_expands_relative_include_within_its_layer(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write(workspace / ".newcode" / "INSTRUCTIONS.md", "before\n@include nested/rules.md\nafter")
    _write(workspace / ".newcode" / "nested" / "rules.md", "included")

    result = load_project_instructions(workspace, user_root=tmp_path / "empty-user")

    assert _contents(result) == [
        (InstructionSource.WORKSPACE, "before\nincluded\nafter"),
    ]


def test_include_accepts_five_nested_levels(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / ".newcode"
    _write(root / "INSTRUCTIONS.md", "@include one.md")
    for number in range(1, MAX_INCLUDE_DEPTH + 1):
        content = f"level {number}"
        if number < MAX_INCLUDE_DEPTH:
            content += f"\n@include level-{number + 1}.md"
        _write(root / ("one.md" if number == 1 else f"level-{number}.md"), content)

    result = load_project_instructions(workspace, user_root=tmp_path / "empty-user")

    assert result.instructions[0].content.endswith("level 5")
    assert result.errors == ()


def test_rejects_sixth_include_level_without_looping(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / ".newcode"
    _write(root / "INSTRUCTIONS.md", "@include level-1.md")
    for number in range(1, MAX_INCLUDE_DEPTH + 2):
        content = f"level {number}"
        if number < MAX_INCLUDE_DEPTH + 1:
            content += f"\n@include level-{number + 1}.md"
        _write(root / f"level-{number}.md", content)

    result = load_project_instructions(workspace, user_root=tmp_path / "empty-user")

    assert "level 5" in result.instructions[0].content
    assert "level 6" not in result.instructions[0].content
    assert [error.code for error in result.errors] == ["instruction_include_depth"]


def test_cycle_is_skipped_once_with_safe_error(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / ".newcode"
    _write(root / "INSTRUCTIONS.md", "root\n@include a.md")
    _write(root / "a.md", "a\n@include b.md")
    _write(root / "b.md", "b\n@include a.md")

    result = load_project_instructions(workspace, user_root=tmp_path / "empty-user")

    assert result.instructions[0].content == "root\na\nb"
    assert [error.code for error in result.errors] == ["instruction_include_cycle"]


@pytest.mark.parametrize("include", ["@include ../outside.md", "@include /outside.md", "@include C:/outside.md"])
def test_rejects_include_path_escape(tmp_path: Path, include: str):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write(workspace / ".newcode" / "INSTRUCTIONS.md", include)

    result = load_project_instructions(workspace, user_root=tmp_path / "empty-user")

    assert result.instructions == ()
    assert [error.code for error in result.errors] == [
        "instruction_path_outside_workspace"
    ]


def test_rejects_symbolic_link_even_when_target_is_inside_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / ".newcode"
    _write(root / "INSTRUCTIONS.md", "@include linked.md")
    _write(root / "actual.md", "unsafe link target")
    link = root / "linked.md"
    try:
        link.symlink_to(root / "actual.md")
    except OSError:
        pytest.skip("当前平台不允许测试符号链接")

    result = load_project_instructions(workspace, user_root=tmp_path / "empty-user")

    assert result.instructions == ()
    assert [error.code for error in result.errors] == [
        "instruction_path_outside_workspace"
    ]


def test_bad_layer_is_isolated_and_diagnostics_do_not_expose_content(tmp_path: Path):
    workspace = tmp_path / "workspace"
    user_root = tmp_path / "user" / ".newcode"
    workspace.mkdir()
    _write(user_root / "INSTRUCTIONS.md", "user remains")
    _write(workspace / ".newcode" / "INSTRUCTIONS.md", "@include not-markdown.txt")
    _write(workspace / "AGENTS.md", "project remains")

    result = load_project_instructions(workspace, user_root=user_root)

    assert _contents(result) == [
        (InstructionSource.PROJECT, "project remains"),
        (InstructionSource.USER, "user remains"),
    ]
    assert [error.code for error in result.errors] == ["instruction_path_outside_workspace"]
    assert all("not-markdown" not in str(error) for error in result.errors)
