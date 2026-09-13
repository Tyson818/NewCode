from __future__ import annotations

from dataclasses import dataclass
import re

from newcode.permissions.types import (
    PermissionDecision,
    PermissionDecisionValue,
    PermissionLayer,
    PermissionRequest,
    RiskLevel,
)


@dataclass(frozen=True)
class DenylistRule:
    id: str
    pattern: re.Pattern[str]
    reason: str


HARD_DENYLIST_RULES: tuple[DenylistRule, ...] = (
    DenylistRule(
        "windows_del_recursive_quiet",
        re.compile(r"\bdel\b(?=[^|;&\n\r]*\s/s\b)(?=[^|;&\n\r]*\s/q\b)", re.IGNORECASE),
        "命令命中 hard denylist: del /s /q",
    ),
    DenylistRule(
        "windows_rmdir_recursive_quiet",
        re.compile(r"\brmdir\b(?=[^|;&\n\r]*\s/s\b)(?=[^|;&\n\r]*\s/q\b)", re.IGNORECASE),
        "命令命中 hard denylist: rmdir /s /q",
    ),
    DenylistRule(
        "windows_remove_item_recurse_force",
        re.compile(
            r"\bremove-item\b(?=[^|;&\n\r]*-(?:r|recurse)\b)(?=[^|;&\n\r]*-force\b)",
            re.IGNORECASE,
        ),
        "命令命中 hard denylist: Remove-Item -Recurse -Force",
    ),
    DenylistRule(
        "windows_format",
        re.compile(r"(^|[|;&]\s*)format(?:\.com|\.exe)?\b", re.IGNORECASE),
        "命令命中 hard denylist: format",
    ),
    DenylistRule(
        "windows_shutdown",
        re.compile(r"(^|[|;&]\s*)shutdown(?:\.exe)?\b", re.IGNORECASE),
        "命令命中 hard denylist: shutdown",
    ),
    DenylistRule(
        "windows_reg_delete",
        re.compile(r"\breg(?:\.exe)?\s+delete\b", re.IGNORECASE),
        "命令命中 hard denylist: reg delete",
    ),
    DenylistRule(
        "windows_curl_pipe_powershell",
        re.compile(r"\bcurl(?:\.exe)?\b[^|]*\|\s*(?:powershell|pwsh)(?:\.exe)?\b", re.IGNORECASE),
        "命令命中 hard denylist: curl ... | powershell",
    ),
    DenylistRule(
        "windows_iwr_pipe_iex",
        re.compile(r"\b(?:iwr|invoke-webrequest)\b[^|]*\|\s*(?:iex|invoke-expression)\b", re.IGNORECASE),
        "命令命中 hard denylist: iwr ... | iex",
    ),
    DenylistRule(
        "posix_rm_rf_root",
        re.compile(r"\brm\b(?=[^|;&\n\r]*-[^\s]*r)(?=[^|;&\n\r]*-[^\s]*f)[^|;&\n\r]*\s/(?:\*|\s*$|$)", re.IGNORECASE),
        "命令命中 hard denylist: rm -rf /",
    ),
    DenylistRule(
        "posix_sudo_rm_rf",
        re.compile(r"\bsudo\s+rm\b(?=[^|;&\n\r]*-[^\s]*r)(?=[^|;&\n\r]*-[^\s]*f)", re.IGNORECASE),
        "命令命中 hard denylist: sudo rm -rf",
    ),
    DenylistRule(
        "posix_mkfs",
        re.compile(r"(^|[|;&]\s*)mkfs(?:\.[\w-]+)?\b", re.IGNORECASE),
        "命令命中 hard denylist: mkfs",
    ),
    DenylistRule(
        "posix_dd_to_dev",
        re.compile(r"\bdd\b(?=[^|;&\n\r]*\bif=)(?=[^|;&\n\r]*\bof=/dev/)", re.IGNORECASE),
        "命令命中 hard denylist: dd if=... of=/dev/...",
    ),
    DenylistRule(
        "posix_chmod_recursive_777_root",
        re.compile(
            r"\bchmod\b(?=[^|;&\n\r]*-(?:[A-Za-z]*R[A-Za-z]*|recursive)\b)[^|;&\n\r]*\s777\s+/",
            re.IGNORECASE,
        ),
        "命令命中 hard denylist: chmod -R 777 /",
    ),
    DenylistRule(
        "posix_curl_pipe_sh",
        re.compile(r"\bcurl\b[^|]*\|\s*(?:sh|bash)\b", re.IGNORECASE),
        "命令命中 hard denylist: curl ... | sh",
    ),
    DenylistRule(
        "posix_wget_pipe_bash",
        re.compile(r"\bwget\b[^|]*\|\s*(?:bash|sh)\b", re.IGNORECASE),
        "命令命中 hard denylist: wget ... | bash",
    ),
)


def find_hard_denylist_match(command: str) -> DenylistRule | None:
    for rule in HARD_DENYLIST_RULES:
        if rule.pattern.search(command):
            return rule
    return None


def check_hard_denylist(request: PermissionRequest) -> PermissionDecision | None:
    if request.tool_name != "run_command":
        return None

    command = request.normalized_args.get("command")
    if not isinstance(command, str):
        return None

    rule = find_hard_denylist_match(command)
    if rule is None:
        return None

    return PermissionDecision(
        decision=PermissionDecisionValue.DENY,
        tool_name=request.tool_name,
        reason=rule.reason,
        risk_level=RiskLevel.HIGH,
        matched_rule=rule.id,
        layer=PermissionLayer.HARD_DENYLIST,
        original_args=request.original_args,
        normalized_args=request.normalized_args,
    )
