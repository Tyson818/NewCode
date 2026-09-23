from __future__ import annotations

from pathlib import Path

from newcode.skills.discovery import SkillDiscovery
from newcode.skills.types import SkillSource


def _skill(name: str, description: str = "Safe short description.", *, tools: str = "- read_file") -> str:
    return f"---\nname: {name}\ndescription: {description}\ntools:\n{tools}\nmode: shared\n---\n\nsecret SOP body\n"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_project_overrides_user_and_builtin_with_deterministic_catalog(tmp_path: Path):
    workspace = tmp_path / "workspace"
    user_home = tmp_path / "home"
    builtin = tmp_path / "builtin"
    _write(builtin / "deploy.md", _skill("deploy", "Builtin description."))
    _write(user_home / ".newcode" / "skills" / "deploy.md", _skill("deploy", "User description."))
    _write(workspace / ".newcode" / "skills" / "deploy.md", _skill("deploy", "Project description."))
    _write(user_home / ".newcode" / "skills" / "lint.md", _skill("lint", "Lint description."))

    catalog = SkillDiscovery(builtin).discover(workspace, user_home=user_home)

    assert [(item.frontmatter.name, item.frontmatter.description, item.source) for item in catalog.skills] == [
        ("deploy", "Project description.", SkillSource.PROJECT),
        ("lint", "Lint description.", SkillSource.USER),
    ]
    assert catalog.diagnostics == ()


def test_bad_single_file_is_isolated_without_exposing_its_content(tmp_path: Path):
    workspace = tmp_path / "workspace"
    skills = workspace / ".newcode" / "skills"
    _write(skills / "valid.md", _skill("valid"))
    _write(skills / "broken.md", "---\nname: broken\nsecret: must-not-leak\n---\nbody")

    catalog = SkillDiscovery(tmp_path / "builtin").discover(workspace, user_home=tmp_path / "home")

    assert [item.frontmatter.name for item in catalog.skills] == ["valid"]
    assert [item.code for item in catalog.diagnostics] == ["skill_frontmatter_invalid"]
    assert "must-not-leak" not in str(catalog.diagnostics)


def test_startup_directory_contains_only_name_and_description(tmp_path: Path):
    workspace = tmp_path / "workspace"
    _write(
        workspace / ".newcode" / "skills" / "private.md",
        _skill("private", "Only this description is visible.", tools="- run_command"),
    )

    catalog = SkillDiscovery(tmp_path / "builtin").discover(workspace, user_home=tmp_path / "home")
    entry = catalog.startup_directory()[0]

    assert entry.name == "private"
    assert entry.description == "Only this description is visible."
    assert set(vars(entry)) == {"name", "description"}
    assert "run_command" not in str(entry)
    assert "secret SOP body" not in str(entry)


def test_default_builtin_root_discovers_packaged_templates(tmp_path: Path):
    workspace = tmp_path / "workspace"
    catalog = SkillDiscovery().discover(workspace, user_home=tmp_path / "home")

    templates = {item.frontmatter.name: item for item in catalog.skills}
    assert set(templates) >= {"commit", "review", "test"}
    assert all(templates[name].source is SkillSource.BUILTIN for name in ("commit", "review", "test"))
    assert templates["commit"].frontmatter.tools == ("read_file", "find_files", "search_code", "run_command")
    assert templates["review"].frontmatter.tools == ("read_file", "find_files", "search_code")
    assert templates["test"].frontmatter.tools == ("read_file", "find_files", "search_code", "run_command")
    assert all(templates[name].entry.name == "SKILL.md" for name in ("commit", "review", "test"))
    assert all("newcode" in templates[name].entry.parts for name in ("commit", "review", "test"))
