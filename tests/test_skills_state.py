from __future__ import annotations

from pathlib import Path

from newcode.skills.discovery import SkillDiscovery
from newcode.skills.loader import SkillLoader
from newcode.skills.state import ActiveSkillState


def _write(path: Path, name: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {name} description.\ntools:\n- read_file\nmode: shared\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _catalog(workspace: Path):
    return SkillDiscovery(workspace / "builtin").discover(workspace, user_home=workspace / "home")


def _loaded(workspace: Path, name: str):
    return SkillLoader().load(name, {}, _catalog(workspace), available_tools={"read_file"})


def test_activation_is_stable_ordered_snapshot_and_prompt_contains_full_sop(tmp_path: Path):
    workspace = tmp_path / "workspace"
    root = workspace / ".newcode" / "skills"
    _write(root / "alpha.md", "alpha", "Alpha SOP")
    _write(root / "beta.md", "beta", "Beta SOP")
    state = ActiveSkillState()

    state.activate(_loaded(workspace, "beta"))
    state.activate(_loaded(workspace, "alpha"))

    assert [item.loaded.metadata.frontmatter.name for item in state.activations] == ["beta", "alpha"]
    prompt = state.prompt_background()
    assert prompt.index("Beta SOP") < prompt.index("Alpha SOP")
    assert "受控 Skill 指令" in prompt and "不授予权限" in prompt


def test_refresh_marks_changed_skill_stale_but_keeps_loaded_snapshot(tmp_path: Path):
    workspace = tmp_path / "workspace"
    path = workspace / ".newcode" / "skills" / "alpha.md"
    _write(path, "alpha", "Old SOP")
    state = ActiveSkillState()
    state.activate(_loaded(workspace, "alpha"))
    _write(path, "alpha", "New SOP")

    state.refresh(_catalog(workspace))

    assert state.activations[0].stale is True
    assert "Old SOP" in state.prompt_background()
    assert "New SOP" not in state.prompt_background()


def test_refresh_removes_deleted_or_invalid_skill_and_reset_paths_clear_state(tmp_path: Path):
    workspace = tmp_path / "workspace"
    path = workspace / ".newcode" / "skills" / "alpha.md"
    _write(path, "alpha", "SOP")
    state = ActiveSkillState()
    state.activate(_loaded(workspace, "alpha"))
    path.unlink()
    state.refresh(_catalog(workspace))
    assert state.activations == ()

    _write(path, "alpha", "SOP")
    state.activate(_loaded(workspace, "alpha"))
    state.reset_for_new_session()
    assert state.activations == ()
    state.activate(_loaded(workspace, "alpha"))
    state.reset_for_resume()
    assert state.activations == ()
