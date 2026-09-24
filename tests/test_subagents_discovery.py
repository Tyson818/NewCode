from __future__ import annotations

import os
from pathlib import Path

import pytest

from newcode.subagents.discovery import AgentDiscovery
from newcode.subagents.types import AgentSource


def _definition(
    name: str,
    description: str = "A safe agent.",
    *,
    allow: str = "- read_file",
    deny: str | None = None,
    model: str | None = None,
    iterations: int = 3,
    permission_mode: str = "inherit",
    isolation: str | None = None,
    body: str = "Private role SOP.",
) -> str:
    deny_field = f"  deny:\n{os.linesep.join('  ' + line for line in deny.splitlines())}\n" if deny is not None else ""
    model_field = f"model: {model}\n" if model is not None else ""
    isolation_field = f"isolation: {isolation}\n" if isolation is not None else ""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "tools:\n"
        f"  allow:\n{os.linesep.join('  ' + line for line in allow.splitlines())}\n"
        f"{deny_field}"
        f"{model_field}"
        f"{isolation_field}"
        f"max_iterations: {iterations}\n"
        f"permission_mode: {permission_mode}\n"
        "---\n"
        f"{body}\n"
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _discover(workspace: Path, home: Path, builtin: Path, **kwargs: object):
    return AgentDiscovery(builtin).discover(
        workspace,
        user_home=home,
        available_tools={"read_file", "write_file", "run_command", "mcp__demo__inspect"},
        **kwargs,
    )


def test_source_precedence_invalid_high_source_falls_back_and_order_is_stable(tmp_path: Path):
    workspace, home, builtin, plugin = (tmp_path / part for part in ("workspace", "home", "builtin", "plugin"))
    _write(plugin / "agent.md", _definition("same", "Plugin"))
    _write(builtin / "agent.md", _definition("same", "Built-in"))
    _write(home / ".newcode" / "agents" / "agent.md", _definition("same", "User"))
    _write(workspace / ".newcode" / "agents" / "agent.md", "---\nname: same\nbad: true\n---\ninvalid")
    _write(workspace / ".newcode" / "agents" / "zeta.md", _definition("zeta"))

    catalog = _discover(workspace, home, builtin, plugin_roots=[plugin])

    assert [(item.name, item.description, item.source) for item in catalog.definitions] == [
        ("same", "User", AgentSource.USER),
        ("zeta", "A safe agent.", AgentSource.PROJECT),
    ]
    assert [item.name for item in catalog.definitions] == [
        item.name for item in _discover(workspace, home, builtin, plugin_roots=[plugin]).definitions
    ]
    assert any(item.code == "subagent_definition_invalid" and item.source is AgentSource.PROJECT for item in catalog.diagnostics)


def test_valid_project_definition_overrides_each_lower_source(tmp_path: Path):
    workspace, home, builtin, plugin = (tmp_path / part for part in ("workspace", "home", "builtin", "plugin"))
    _write(plugin / "agent.md", _definition("same", "Plugin"))
    _write(builtin / "agent.md", _definition("same", "Built-in"))
    _write(home / ".newcode" / "agents" / "agent.md", _definition("same", "User"))
    _write(workspace / ".newcode" / "agents" / "agent.md", _definition("same", "Project"))

    catalog = _discover(workspace, home, builtin, plugin_roots=[plugin])
    assert [(item.name, item.description, item.source) for item in catalog.definitions] == [
        ("same", "Project", AgentSource.PROJECT)
    ]


def test_same_source_duplicate_names_are_all_isolated(tmp_path: Path):
    workspace, home, builtin = tmp_path / "workspace", tmp_path / "home", tmp_path / "builtin"
    root = workspace / ".newcode" / "agents"
    _write(root / "a.md", _definition("duplicate", "First"))
    _write(root / "b.md", _definition("duplicate", "Second"))
    _write(home / ".newcode" / "agents" / "fallback.md", _definition("duplicate", "User fallback"))

    catalog = _discover(workspace, home, builtin)
    assert [(item.name, item.description, item.source) for item in catalog.definitions] == [
        ("duplicate", "User fallback", AgentSource.USER)
    ]
    conflicts = [item for item in catalog.diagnostics if item.code == "subagent_definition_conflict"]
    assert len(conflicts) == 2
    assert all(item.source is AgentSource.PROJECT and item.name == "duplicate" for item in conflicts)


def test_bad_sibling_unknown_tool_and_duplicate_yaml_key_are_isolated(tmp_path: Path):
    workspace, home, builtin = tmp_path / "workspace", tmp_path / "home", tmp_path / "builtin"
    root = workspace / ".newcode" / "agents"
    _write(root / "good.md", _definition("good"))
    _write(root / "unknown.md", _definition("unknown", allow="- missing_tool"))
    _write(root / "yaml.md", "---\nname: yaml\nname: override\ndescription: bad\ntools:\n  allow: []\nmax_iterations: 1\npermission_mode: inherit\n---\nsecret body")

    catalog = _discover(workspace, home, builtin)
    assert [item.name for item in catalog.definitions] == ["good"]
    codes = {item.code for item in catalog.diagnostics}
    assert codes == {"subagent_tool_unknown", "subagent_definition_invalid"}
    assert "secret body" not in repr(catalog.diagnostics)
    assert str(root) not in repr(catalog.diagnostics)


def test_startup_projection_contains_only_name_and_description(tmp_path: Path):
    workspace, home, builtin = tmp_path / "workspace", tmp_path / "home", tmp_path / "builtin"
    _write(
        workspace / ".newcode" / "agents" / "private.md",
        _definition("private", "Public description.", allow="- run_command", model="private-model", body="secret SOP"),
    )
    catalog = _discover(workspace, home, builtin)
    entry = catalog.startup_directory()[0]

    assert set(vars(entry)) == {"name", "description"}
    assert entry.name == "private"
    assert entry.description == "Public description."
    assert "secret SOP" not in repr(entry)
    assert "private-model" not in repr(entry)
    assert "run_command" not in repr(entry)
    assert str(workspace) not in repr(entry)


@pytest.mark.parametrize(("isolation", "expected"), [(None, "shared"), ("shared", "shared"), ("worktree", "worktree")])
def test_definition_isolation_defaults_and_accepts_supported_values(tmp_path: Path, isolation: str | None, expected: str):
    from newcode.subagents.types import AgentIsolation

    workspace, home, builtin = tmp_path / "workspace", tmp_path / "home", tmp_path / "builtin"
    _write(workspace / ".newcode" / "agents" / "agent.md", _definition("agent", isolation=isolation))
    catalog = _discover(workspace, home, builtin)
    assert len(catalog.definitions) == 1
    assert catalog.definitions[0].isolation is AgentIsolation(expected)


def test_invalid_isolation_isolated_without_shared_fallback(tmp_path: Path):
    workspace, home, builtin = tmp_path / "workspace", tmp_path / "home", tmp_path / "builtin"
    root = workspace / ".newcode" / "agents"
    _write(root / "bad.md", _definition("bad", isolation="worktree-ish"))
    _write(root / "good.md", _definition("good"))
    catalog = _discover(workspace, home, builtin)
    assert [item.name for item in catalog.definitions] == ["good"]
    assert [item.code for item in catalog.diagnostics] == ["subagent_definition_invalid"]


def test_invalid_high_priority_isolation_falls_back_to_valid_lower_source(tmp_path: Path):
    workspace, home, builtin = tmp_path / "workspace", tmp_path / "home", tmp_path / "builtin"
    _write(workspace / ".newcode" / "agents" / "agent.md", _definition("same", "Invalid project", isolation="unsafe"))
    _write(home / ".newcode" / "agents" / "agent.md", _definition("same", "Valid user"))
    catalog = _discover(workspace, home, builtin)
    assert [(item.name, item.description, item.isolation.value) for item in catalog.definitions] == [
        ("same", "Valid user", "shared")
    ]
    assert any(item.code == "subagent_definition_invalid" and item.source is AgentSource.PROJECT for item in catalog.diagnostics)


@pytest.mark.parametrize("field,value", [("allow", "- missing_tool"), ("deny", "- missing_tool")])
def test_tool_references_are_checked_against_injected_registry(tmp_path: Path, field: str, value: str):
    workspace, home, builtin = tmp_path / "workspace", tmp_path / "home", tmp_path / "builtin"
    kwargs = {field: value}
    _write(workspace / ".newcode" / "agents" / "bad.md", _definition("bad", **kwargs))

    catalog = AgentDiscovery(builtin).discover(workspace, user_home=home, available_tools={"read_file"})
    assert catalog.definitions == ()
    assert [item.code for item in catalog.diagnostics] == ["subagent_tool_unknown"]


def test_plugin_roots_are_opt_in_and_have_lowest_precedence(tmp_path: Path):
    workspace, home, builtin, plugin = (tmp_path / part for part in ("workspace", "home", "builtin", "plugin"))
    _write(plugin / "agent.md", _definition("plugin-only", "Plugin"))

    without_plugin = _discover(workspace, home, builtin)
    with_plugin = _discover(workspace, home, builtin, plugin_roots=[plugin])

    assert without_plugin.definitions == ()
    assert [(item.name, item.source) for item in with_plugin.definitions] == [("plugin-only", AgentSource.PLUGIN)]


def test_symlinked_definition_is_rejected_without_leaking_path(tmp_path: Path):
    workspace, home, builtin = tmp_path / "workspace", tmp_path / "home", tmp_path / "builtin"
    outside = tmp_path / "outside.md"
    _write(outside, _definition("outside"))
    entry = workspace / ".newcode" / "agents" / "linked.md"
    entry.parent.mkdir(parents=True)
    try:
        entry.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前 Windows 环境不允许创建符号链接：{type(exc).__name__}")

    catalog = _discover(workspace, home, builtin)
    assert catalog.definitions == ()
    assert any(item.code == "subagent_path_unsafe" for item in catalog.diagnostics)
    assert str(outside) not in repr(catalog.diagnostics)


def test_symlinked_root_is_rejected(tmp_path: Path):
    workspace, home, builtin = tmp_path / "workspace", tmp_path / "home", tmp_path / "builtin"
    actual = tmp_path / "actual"
    actual.mkdir()
    workspace.mkdir()
    try:
        (workspace / ".newcode").symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前 Windows 环境不允许创建符号链接：{type(exc).__name__}")

    catalog = _discover(workspace, home, builtin)
    assert catalog.definitions == ()
    assert any(item.code == "subagent_path_unsafe" for item in catalog.diagnostics)


def test_non_directory_source_root_is_isolated(tmp_path: Path):
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    builtin = tmp_path / "builtin-file"
    builtin.write_text("not a directory", encoding="utf-8")
    catalog = _discover(workspace, home, builtin)
    assert catalog.definitions == ()
    assert any(item.code == "subagent_path_unsafe" and item.source is AgentSource.BUILTIN for item in catalog.diagnostics)
