from __future__ import annotations

from pathlib import Path

from newcode.commands.builtins import create_builtin_registry
from newcode.commands.dispatcher import CommandDispatcher
from newcode.commands.types import CommandOutcomeKind, CommandParseKind
from newcode.commands.ui import FakeUIControl
from newcode.skills.commands import SkillCommandDispatcher, SkillCommandOverlay
from newcode.skills.discovery import SkillDiscovery
from newcode.skills.loader import SkillLoader
from newcode.skills.state import ActiveSkillState


def _activate(workspace: Path, state: ActiveSkillState, name: str) -> None:
    path = workspace / ".newcode" / "skills" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {name} description.\ntools:\n- read_file\nmode: shared\nparameters:\n- branch\n---\n\n{name} SOP {{branch}}\n",
        encoding="utf-8",
    )
    catalog = SkillDiscovery(workspace / "builtin").discover(workspace, user_home=workspace / "home")
    state.activate(SkillLoader().load(name, {"branch": "main"}, catalog, available_tools={"read_file"}))


def test_static_review_wins_while_review_skill_remains_active_without_overlay(tmp_path: Path):
    state = ActiveSkillState()
    _activate(tmp_path, state, "review")
    registry = create_builtin_registry()
    overlay = SkillCommandOverlay(registry, state)
    dispatcher = SkillCommandDispatcher(CommandDispatcher(registry), overlay)

    parsed = dispatcher.parse("/review")

    assert parsed.kind is CommandParseKind.COMMAND
    assert parsed.command is not None and parsed.command.definition.name == "/review"
    assert overlay.definitions() == ()
    assert state.activations[0].loaded.metadata.frontmatter.name == "review"


def test_non_conflicting_overlay_emits_pending_request_and_clear_removes_it(tmp_path: Path):
    state = ActiveSkillState()
    _activate(tmp_path, state, "demo")
    registry = create_builtin_registry()
    dispatcher = SkillCommandDispatcher(CommandDispatcher(registry), SkillCommandOverlay(registry, state))
    ui = FakeUIControl()

    parsed = dispatcher.dispatch_input("/demo branch=release", ui)
    assert parsed.kind is CommandParseKind.COMMAND and parsed.command is not None
    outcome = dispatcher.execute(parsed.command, object(), ui)
    assert outcome.kind is CommandOutcomeKind.SKILL_REQUEST
    assert outcome.skill_name == "demo"
    assert outcome.skill_parameters == (("branch", "release"),)

    state.clear()
    assert dispatcher.parse("/demo branch=release").kind is CommandParseKind.UNKNOWN
