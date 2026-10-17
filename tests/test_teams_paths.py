import ctypes
from ctypes import wintypes as wt
import os
from pathlib import Path
import struct

import pytest

from newcode.teams.config import load_team_config
from newcode.teams.paths import TeamPaths, _win, verify_private, workspace_binding
from newcode.teams.types import TeamError, new_id


def make_paths(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    common = workspace / ".git"
    common.mkdir(exist_ok=True)
    result = TeamPaths(workspace, common_git_dir=common, user_home=home)
    result.ensure_root()
    return result


def make_directory_link(link: Path, target: Path):
    """Windows 实际创建 junction，不依赖符号链接特权或 fake lstat。"""
    if os.name != "nt":
        link.symlink_to(target, target_is_directory=True)
        return
    kernel, _ = _win()
    link.mkdir()
    substitute = ("\\??\\" + str(target.resolve())).encode("utf-16-le")
    printable = str(target.resolve()).encode("utf-16-le")
    payload = substitute + b"\0\0" + printable + b"\0\0"
    buffer = struct.pack("<LHHHHHH", 0xA0000003, 8 + len(payload), 0, 0, len(substitute), len(substitute) + 2, len(printable)) + payload
    handle = kernel.CreateFileW(str(link), 0x40000000, 7, None, 3, 0x02200000, None)
    assert handle != ctypes.c_void_p(-1).value
    kernel.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    try:
        returned = wt.DWORD()
        assert kernel.DeviceIoControl(handle, 0x900A4, buffer, len(buffer), None, 0, ctypes.byref(returned), None), ctypes.get_last_error()
    finally:
        kernel.CloseHandle(handle)


def set_public_acl(path: Path):
    if os.name != "nt":
        path.chmod(0o777)
        return
    kernel, security = _win()
    descriptor = ctypes.c_void_p()
    assert security.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:P(A;OICI;FA;;;WD)", 1, ctypes.byref(descriptor), None)
    security.SetFileSecurityW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.c_void_p]
    try:
        assert security.SetFileSecurityW(str(path), 0x80000004, descriptor), ctypes.get_last_error()
    finally:
        kernel.LocalFree(descriptor)


def test_real_platform_private_acl_and_canonical_workspace_binding(tmp_path):
    paths = make_paths(tmp_path)
    verify_private(paths.root)
    assert paths.root == paths.home / ".newcode" / "teams"
    assert paths.workspace_fingerprint == workspace_binding(paths.workspace_root, paths.common_git_dir)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    assert workspace_binding(sibling, paths.common_git_dir) != paths.workspace_fingerprint
    assert workspace_binding(paths.workspace_root, sibling) != paths.workspace_fingerprint


def test_real_platform_public_acl_refused_before_creating_child(tmp_path):
    paths = make_paths(tmp_path)
    set_public_acl(paths.root)
    with pytest.raises(TeamError, match="team_store_unavailable"):
        paths.mkdir(new_id("team"))
    assert list(paths.root.iterdir()) == []


def test_real_junction_outside_team_root_rejected_without_target_writes(tmp_path):
    paths = make_paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")
    team_id = new_id("team")
    link = paths.root / team_id
    make_directory_link(link, outside)
    assert link.resolve() == outside.resolve()
    with pytest.raises(TeamError, match="team_path_invalid"):
        paths.mkdir(team_id, "transactions")
    assert sentinel.read_text(encoding="utf-8") == "original"
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]


def test_existing_team_root_replaced_by_real_junction_is_refused(tmp_path):
    paths = make_paths(tmp_path)
    saved = paths.root.with_name("saved-teams")
    paths.root.rename(saved)
    outside = tmp_path / "outside"
    outside.mkdir()
    make_directory_link(paths.root, outside)
    with pytest.raises(TeamError):
        paths.ensure_root()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("part", ["..", "../escape", "C:escape", "\\escape", "/escape", "a/b", "a\\b", "con", "foo."])
def test_invalid_path_zero_outside_write(tmp_path, part):
    paths = make_paths(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(TeamError):
        paths.mkdir(part)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert list(paths.root.iterdir()) == []


def test_hardlink_to_outside_regular_file_is_refused(tmp_path):
    paths = make_paths(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    link = paths.root / "alias.json"
    os.link(outside, link)
    with pytest.raises(TeamError):
        paths.checked("alias.json")
    assert outside.read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(os.name != "nt", reason="Windows目录句柄不共享DELETE的真实平台测试")
def test_actual_windows_guard_prevents_team_root_rename(tmp_path):
    paths = make_paths(tmp_path)
    with paths.guard():
        with pytest.raises(OSError):
            paths.root.rename(paths.root.with_name("replacement"))
        verify_private(paths.root)
    assert paths.root.is_dir() and not paths.root.with_name("replacement").exists()


def test_config_defaults_narrowing_and_dual_coordinator_authorization(tmp_path):
    paths = make_paths(tmp_path)
    assert not load_team_config(paths.workspace_root, user_home=paths.home).config.enabled
    user = paths.home / ".newcode" / "teams.yaml"
    user.write_text("enabled: true\nmax_members: 6\nmax_running_members: 3\ncoordinator:\n  enabled: true\nrequire_plan_approval: false\n", encoding="utf-8")
    project_dir = paths.workspace_root / ".newcode"
    project_dir.mkdir()
    project = project_dir / "teams.yaml"
    project.write_text("max_members: 2\nmax_running_members: 1\nrequire_plan_approval: true\n", encoding="utf-8")
    result = load_team_config(paths.workspace_root, user_home=paths.home)
    assert result.diagnostics == ()
    assert (result.config.max_members, result.config.max_running_members, result.config.require_plan_approval) == (2, 1, True)
    assert not result.config.coordinator_active({})
    assert result.config.coordinator_active({"NEWCODE_TEAM_COORDINATOR": "1"})
    assert not result.config.coordinator_active({"NEWCODE_TEAM_COORDINATOR": "true"})


@pytest.mark.parametrize("project", ["enabled: true", "coordinator: {enabled: true}", "max_members: 8", "require_plan_approval: false", "backend_default: pane", "unknown: secret"])
def test_project_cannot_expand_user_configuration(tmp_path, project):
    paths = make_paths(tmp_path)
    (paths.home / ".newcode" / "teams.yaml").write_text("enabled: false\nmax_members: 2\n", encoding="utf-8")
    folder = paths.workspace_root / ".newcode"
    folder.mkdir()
    (folder / "teams.yaml").write_text(project, encoding="utf-8")
    result = load_team_config(paths.workspace_root, user_home=paths.home)
    assert result.diagnostics == (("team_config_invalid", "project"),)
    assert not result.config.enabled and result.config.max_members == 2
    assert not result.config.coordinator_enabled


@pytest.mark.parametrize("value", ["enabled: true\nenabled: false", "enabled: 1", "max_members: true", "base: &anchor {}\ncopy: *anchor", "- not-a-mapping"])
def test_invalid_user_yaml_is_disabled_with_safe_diagnostic(tmp_path, value):
    paths = make_paths(tmp_path)
    (paths.home / ".newcode" / "teams.yaml").write_text(value, encoding="utf-8")
    result = load_team_config(paths.workspace_root, user_home=paths.home)
    assert not result.config.enabled
    assert result.diagnostics == (("team_config_invalid", "user"),)
