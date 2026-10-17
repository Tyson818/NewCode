"""受控 Team 根、真实平台 ACL 与写入期间的路径校验。"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes as wt
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .types import TeamError, validate_id


@lru_cache(maxsize=1)
def _win():
    if os.name != "nt":
        raise TeamError("team_store_unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    kernel.CreateFileW.restype = wt.HANDLE
    kernel.CreateDirectoryW.argtypes = [wt.LPCWSTR, ctypes.c_void_p]
    kernel.MoveFileExW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
    kernel.GetVolumePathNameW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD]
    kernel.GetVolumeInformationW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]
    kernel.GetDriveTypeW.argtypes = [wt.LPCWSTR]
    security.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    security.GetTokenInformation.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    security.GetNamedSecurityInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    security.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, wt.DWORD]
    security.GetAce.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    return kernel, security


def _sid_text(security, kernel, sid) -> str:
    output = wt.LPWSTR()
    if not security.ConvertSidToStringSidW(sid, ctypes.byref(output)):
        raise TeamError("team_store_unavailable")
    try:
        return output.value
    finally:
        kernel.LocalFree(output)


def current_user_sid() -> str:
    kernel, security = _win()
    token = wt.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
        raise TeamError("team_store_unavailable")
    try:
        size = wt.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise TeamError("team_store_unavailable")
        buffer = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise TeamError("team_store_unavailable")
        return _sid_text(security, kernel, ctypes.c_void_p.from_buffer(buffer).value)
    finally:
        kernel.CloseHandle(token)


def verify_private(path: Path) -> None:
    """读取真实 owner/DACL；未知 ACE 或空 DACL 一律拒绝。"""
    if os.name == "posix":
        info = path.lstat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise TeamError("team_store_unavailable")
        return
    kernel, security = _win()
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    result = security.GetNamedSecurityInfoW(str(path), 1, 0x5, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor))
    if result != 0 or not descriptor.value:
        raise TeamError("team_store_unavailable")
    try:
        user = current_user_sid()
        trusted = {user, "S-1-5-18", "S-1-5-32-544"}
        if not owner.value or _sid_text(security, kernel, owner) not in trusted or not acl.value:
            raise TeamError("team_store_unavailable")
        class AclSize(ctypes.Structure):
            _fields_ = [("count", wt.DWORD), ("used", wt.DWORD), ("free", wt.DWORD)]
        size = AclSize()
        if not security.GetAclInformation(acl, ctypes.byref(size), ctypes.sizeof(size), 2) or not size.count:
            raise TeamError("team_store_unavailable")
        has_user = False
        for index in range(size.count):
            ace = ctypes.c_void_p()
            if not security.GetAce(acl, index, ctypes.byref(ace)):
                raise TeamError("team_store_unavailable")
            # 只接受标准 ACCESS_ALLOWED_ACE，复杂或未知 ACL 不推测为安全。
            if ctypes.c_ubyte.from_address(ace.value).value != 0:
                raise TeamError("team_store_unavailable")
            principal = _sid_text(security, kernel, ace.value + 8)
            if principal not in trusted:
                raise TeamError("team_store_unavailable")
            has_user |= principal == user
        if not has_user:
            raise TeamError("team_store_unavailable")
    finally:
        kernel.LocalFree(descriptor)


def _mkdir_private(path: Path) -> None:
    if os.name == "posix":
        path.mkdir(mode=0o700)
        return
    kernel, security = _win()
    descriptor = ctypes.c_void_p()
    user = current_user_sid()
    sddl = f"O:{user}D:P(A;OICI;FA;;;{user})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise TeamError("team_store_unavailable")
    class Attributes(ctypes.Structure):
        _fields_ = [("length", wt.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wt.BOOL)]
    try:
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            if ctypes.get_last_error() != 183:
                raise TeamError("team_store_unavailable")
    finally:
        kernel.LocalFree(descriptor)


def reject_links(path: Path, *, missing: bool = False) -> None:
    """逐段 lstat，不能先 resolve 再漏掉原始 reparse point。"""
    raw = Path(path)
    if ".." in raw.parts or not raw.is_absolute() or str(raw).startswith("\\\\"):
        raise TeamError("team_path_invalid")
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise TeamError("team_path_invalid") from None
        except OSError:
            raise TeamError("team_path_invalid") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise TeamError("team_path_invalid")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise TeamError("team_path_invalid")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise TeamError("team_path_invalid")


def canonical_directory(path: Path) -> Path:
    raw = Path(path).absolute()
    reject_links(raw)
    if not raw.is_dir():
        raise TeamError("team_path_invalid")
    return raw.resolve(strict=True)


def workspace_binding(workspace_root: Path, common_git_dir: Path) -> str:
    """common-dir 来自可信调用者；本阶段不启动 Git 或推测 .git 指针。"""
    roots = [os.path.normcase(str(canonical_directory(root))) for root in (workspace_root, common_git_dir)]
    return hashlib.sha256(json.dumps(roots, ensure_ascii=False).encode("utf-8")).hexdigest()


class TeamPaths:
    def __init__(self, workspace_root: Path, *, common_git_dir: Path, user_home: Path | None = None) -> None:
        self.home = canonical_directory(Path.home() if user_home is None else user_home)
        self.workspace_root = canonical_directory(workspace_root)
        self.common_git_dir = canonical_directory(common_git_dir)
        self.workspace_fingerprint = workspace_binding(self.workspace_root, self.common_git_dir)
        self.root = self.home / ".newcode" / "teams"

    def ensure_root(self) -> Path:
        for path in (self.root.parent, self.root):
            reject_links(path, missing=True)
            if not path.exists():
                _mkdir_private(path)
            reject_links(path)
            verify_private(path)
        return self.root

    def checked(self, *parts: str, missing: bool = False) -> Path:
        for part in parts:
            if not isinstance(part, str) or part in {"", ".", ".."} or part.endswith(".") or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", part):
                raise TeamError("team_path_invalid")
            if part.split(".")[0].casefold() in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}:
                raise TeamError("team_path_invalid")
        path = self.root.joinpath(*parts)
        reject_links(self.root, missing=missing)
        reject_links(path, missing=missing)
        if not path.resolve(strict=False).is_relative_to(self.root.resolve(strict=False)):
            raise TeamError("team_path_invalid")
        current = self.root
        for part in (None, *parts):
            if part is not None:
                current /= part
            if current.exists():
                verify_private(current)
        return path

    def team(self, team_id: str, *, missing: bool = False) -> Path:
        return self.checked(validate_id(team_id, "team"), missing=missing)

    def mkdir(self, *parts: str) -> Path:
        self.checked(*parts, missing=True)
        for index in range(1, len(parts) + 1):
            path = self.checked(*parts[:index], missing=True)
            if not path.exists():
                _mkdir_private(path)
            self.checked(*parts[:index])
        return self.checked(*parts)

    @contextmanager
    def guard(self, *parts: str):
        """Windows 上持有目录句柄且不共享 DELETE，阻止写入中换根。"""
        path = self.checked(*parts, missing=True)
        handles = []
        try:
            if os.name == "nt":
                kernel, _ = _win()
                chain = [self.root.parent, self.root]
                chain.extend(self.root.joinpath(*parts[:i]) for i in range(1, len(parts) + 1))
                for directory in chain:
                    if not directory.exists():
                        break
                    if not directory.is_dir():
                        continue
                    # 仅 READ_ATTRIBUTES 在 Windows 上不足以约束 rename 共享；
                    # 必须取得真实读访问，且不授予 FILE_SHARE_DELETE。
                    handle = kernel.CreateFileW(str(directory), 0x80000000, 0x3, None, 3, 0x02200000, None)
                    if handle == ctypes.c_void_p(-1).value:
                        raise TeamError("team_store_unavailable")
                    handles.append(handle)
            self.checked(*parts, missing=True)
            yield path
        finally:
            if os.name == "nt":
                for handle in reversed(handles):
                    kernel.CloseHandle(handle)


def durable_publish(source: Path, destination: Path, *, replace: bool) -> None:
    """先 fsync 文件，再使用本地平台的持久化 rename；未知文件系统拒绝。"""
    if source.parent != destination.parent:
        raise TeamError("team_path_invalid")
    if os.name == "nt":
        kernel, _ = _win()
        volume = ctypes.create_unicode_buffer(260)
        filesystem = ctypes.create_unicode_buffer(64)
        if not kernel.GetVolumePathNameW(str(source), volume, 260) or kernel.GetDriveTypeW(volume) != 3:
            raise TeamError("team_store_unavailable")
        if not kernel.GetVolumeInformationW(volume, None, 0, None, None, None, filesystem, 64) or filesystem.value != "NTFS":
            raise TeamError("team_store_unavailable")
        # MOVEFILE_WRITE_THROUGH 与同目录移动；不设置跨卷 COPY_ALLOWED。
        if not kernel.MoveFileExW(str(source), str(destination), 0x8 | (0x1 if replace else 0)):
            raise TeamError("team_store_unavailable")
    elif os.name == "posix":
        if replace:
            os.replace(source, destination)
        else:
            os.link(source, destination, follow_symlinks=False)
            source.unlink()
        fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        raise TeamError("team_store_unavailable")
