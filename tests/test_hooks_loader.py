"""Hook YAML 加载、路径和 user/project 权限合并。"""

from pathlib import Path

import pytest

from newcode.hooks import HookSource, load_hook_rules


def _write(root: Path, content: str) -> Path:
    directory = root / ".newcode"
    directory.mkdir(exist_ok=True)
    path = directory / "hooks.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def _load(tmp_path: Path, user: str | None = None, project: str | None = None):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    if user is not None:
        _write(home, user)
    if project is not None:
        _write(workspace, project)
    return load_hook_rules(workspace, user_home=home)


def test_invalid_rule_isolated_and_project_override_order(tmp_path):
    result = _load(tmp_path,
        """hooks:
  - {id: first, event: turn_start, action: {type: subagent}}
  - {id: replace, event: turn_start, action: {type: subagent}}
  - {id: invalid, event: not_real, action: {type: subagent}}
  - {id: first, event: turn_end, action: {type: subagent}}
""",
        """hooks:
  - {id: replace, event: turn_end, action: {type: subagent}}
  - {id: project_only, event: session_end, action: {type: subagent}}
""")
    assert [(rule.id, rule.source) for rule in result.rules] == [
        ("first", HookSource.USER), ("replace", HookSource.PROJECT),
        ("project_only", HookSource.PROJECT),
    ]
    assert [item.code for item in result.diagnostics] == [
        "hook_event_invalid", "hook_rule_invalid",
    ]


def test_bad_yaml_and_unknown_actions_do_not_block_other_source(tmp_path):
    result = _load(tmp_path, "hooks: [unclosed",
                   "hooks:\n  - {id: good, event: turn_start, action: {type: subagent}}\n"
                   "  - {id: bad, event: turn_start, action: {type: unknown}}\n")
    assert [rule.id for rule in result.rules] == ["good"]
    assert {item.code for item in result.diagnostics} == {
        "hook_config_invalid", "hook_action_invalid",
    }


def test_project_cannot_enable_or_expand_network(tmp_path):
    result = _load(tmp_path,
        "network:\n  enabled: true\n  allow_hosts: [example.com]\nhooks: []\n",
        """network:
  enabled: true
  allow_hosts: [evil.example]
hooks:
  - {id: allowed, event: turn_start, action: {type: http_request, url: 'https://example.com/a'}}
  - {id: denied, event: turn_start, action: {type: http_request, url: 'https://evil.example/a'}}
""")
    assert result.network.allow_hosts == ("example.com",)
    assert [rule.id for rule in result.rules] == ["allowed"]
    assert {item.code for item in result.diagnostics} == {
        "hook_config_invalid", "hook_http_denied",
    }


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "10.0.0.1", "169.254.1.1", "0.0.0.0"])
def test_nonpublic_host_is_rejected_without_network(tmp_path, host):
    result = _load(tmp_path, f"network:\n  enabled: true\n  allow_hosts: [{host}]\n"
                   "hooks:\n  - {id: fetch, event: turn_start, action: {type: http_request, url: 'https://example.com/'}}\n")
    assert not result.rules
    assert not result.network.enabled
    assert "hook_config_invalid" in {item.code for item in result.diagnostics}


def test_credential_header_and_url_are_invalid(tmp_path):
    result = _load(tmp_path,
        "network:\n  enabled: true\n  allow_hosts: [example.com]\n"
        "hooks:\n"
        "  - {id: header, event: turn_start, action: {type: http_request, url: 'https://example.com/', headers: {Authorization: token}}}\n"
        "  - {id: credential, event: turn_start, action: {type: http_request, url: 'https://user:password@example.com/'}}\n")
    assert not result.rules
    assert [item.code for item in result.diagnostics] == ["hook_action_invalid"] * 2
    assert "password" not in str(result.diagnostics)


def test_non_regular_config_is_rejected(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (workspace / ".newcode").mkdir()
    (workspace / ".newcode" / "hooks.yaml").mkdir()
    result = load_hook_rules(workspace, user_home=home)
    assert not result.rules
    assert "hook_config_invalid" in {item.code for item in result.diagnostics}


def test_symlink_escape_is_rejected(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.yaml"
    home.mkdir()
    workspace.mkdir()
    outside.write_text("hooks: []", encoding="utf-8")
    (workspace / ".newcode").mkdir()
    try:
        (workspace / ".newcode" / "hooks.yaml").symlink_to(outside)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"此环境不允许创建符号链接: {type(error).__name__}")
    result = load_hook_rules(workspace, user_home=home)
    assert "hook_config_invalid" in {item.code for item in result.diagnostics}


def test_symlink_config_guard_without_platform_symlink_privilege(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    file_path = _write(workspace, "hooks: []\n")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == file_path or original(path))
    result = load_hook_rules(workspace, user_home=home)
    assert "hook_config_invalid" in {item.code for item in result.diagnostics}


def test_parent_path_component_is_rejected(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    result = load_hook_rules(workspace / ".." / "workspace", user_home=home)
    assert "hook_config_invalid" in {item.code for item in result.diagnostics}
