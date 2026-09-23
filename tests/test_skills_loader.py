from __future__ import annotations

from pathlib import Path

import pytest

from newcode.skills.discovery import SkillDiscovery
from newcode.skills.loader import SkillLoader
from newcode.skills.types import SkillValidationError


def _write_skill(workspace: Path, content: str) -> Path:
    path = workspace / ".newcode" / "skills" / "release.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _catalog(workspace: Path):
    return SkillDiscovery(workspace / "builtin").discover(workspace, user_home=workspace / "home")


def _content(*, body: str = "Prepare {{branch}}.", tools: str = "- read_file", model: str = "") -> str:
    model_line = f"model: {model}\n" if model else ""
    return (
        "---\nname: release\ndescription: Prepare a release.\ntools:\n"
        f"{tools}\nmode: shared\nparameters:\n- branch\n{model_line}---\n\n{body}\n"
    )


def test_loader_reads_full_sop_only_for_explicit_load_and_substitutes_parameters(tmp_path: Path):
    workspace = tmp_path / "workspace"
    _write_skill(workspace, _content(body="Secret SOP: {{branch}}."))
    catalog = _catalog(workspace)

    assert "Secret SOP" not in str(catalog.skills[0])
    loaded = SkillLoader().load("RELEASE", {"branch": "main"}, catalog, available_tools={"read_file"})

    assert loaded.sop == "Secret SOP: main."
    assert loaded.parameters == (("branch", "main"),)


@pytest.mark.parametrize(
    "parameters, code",
    [
        ({}, "skill_parameter_missing"),
        ({"branch": "main", "extra": "x"}, "skill_parameter_invalid"),
        ({"branch": 1}, "skill_parameter_invalid"),
    ],
)
def test_loader_rejects_invalid_parameter_sets(tmp_path: Path, parameters: object, code: str):
    workspace = tmp_path / "workspace"
    _write_skill(workspace, _content())

    with pytest.raises(SkillValidationError, match=code):
        SkillLoader().load("release", parameters, _catalog(workspace), available_tools={"read_file"})


def test_loader_rejects_unknown_tool_and_unavailable_model(tmp_path: Path):
    workspace = tmp_path / "workspace"
    _write_skill(workspace, _content(tools="- run_command", model="controlled-model"))
    catalog = _catalog(workspace)

    with pytest.raises(SkillValidationError, match="skill_tool_unknown"):
        SkillLoader().load("release", {"branch": "main"}, catalog, available_tools={"read_file"})
    with pytest.raises(SkillValidationError, match="skill_model_unavailable"):
        SkillLoader().load("release", {"branch": "main"}, catalog, available_tools={"run_command"})


def test_loader_rejects_changed_entry_instead_of_using_an_unverified_snapshot(tmp_path: Path):
    workspace = tmp_path / "workspace"
    path = _write_skill(workspace, _content())
    catalog = _catalog(workspace)
    path.write_text(_content(body="Changed {{branch}}."), encoding="utf-8")

    with pytest.raises(SkillValidationError, match="skill_load_failed"):
        SkillLoader().load("release", {"branch": "main"}, catalog, available_tools={"read_file"})
