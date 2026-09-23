from __future__ import annotations

from pathlib import Path

import pytest

from newcode.skills.discovery import SkillDiscovery


def _entry(name: str = "package") -> str:
    return f"---\nname: {name}\ndescription: A directory skill.\ntools:\n- read_file\nmode: shared\n---\n\nbody\n"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_directory_skill_uses_skill_md_and_indexes_safe_relative_resources(tmp_path: Path):
    workspace = tmp_path / "workspace"
    package = workspace / ".newcode" / "skills" / "package"
    _write(package / "SKILL.md", _entry())
    _write(package / "templates" / "message.txt", "hello")
    _write(package / "references" / "guide.md", "guide")

    catalog = SkillDiscovery(tmp_path / "builtin").discover(workspace, user_home=tmp_path / "home")

    skill = catalog.skills[0]
    assert skill.entry == package / "SKILL.md"
    assert [resource.relative_path for resource in skill.resources] == [
        "references/guide.md",
        "templates/message.txt",
    ]
    assert all(not Path(resource.relative_path).is_absolute() and ".." not in resource.relative_path for resource in skill.resources)


def test_symbolic_link_resource_rejects_only_unsafe_package(tmp_path: Path):
    workspace = tmp_path / "workspace"
    skills = workspace / ".newcode" / "skills"
    package = skills / "package"
    _write(package / "SKILL.md", _entry())
    _write(skills / "valid.md", "---\nname: valid\ndescription: Valid skill.\ntools:\n- read_file\nmode: shared\n---\nbody")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (package / "escape.txt").symlink_to(outside)
    except OSError:
        pytest.skip("当前平台不允许测试符号链接")

    catalog = SkillDiscovery(tmp_path / "builtin").discover(workspace, user_home=tmp_path / "home")

    assert [skill.frontmatter.name for skill in catalog.skills] == ["valid"]
    assert [item.code for item in catalog.diagnostics] == ["skill_resource_unsafe"]


def test_directory_without_safe_skill_md_is_isolated(tmp_path: Path):
    workspace = tmp_path / "workspace"
    _write(workspace / ".newcode" / "skills" / "missing" / "readme.md", "not an entry")

    catalog = SkillDiscovery(tmp_path / "builtin").discover(workspace, user_home=tmp_path / "home")

    assert catalog.skills == ()
    assert catalog.diagnostics == ()
