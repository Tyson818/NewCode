from __future__ import annotations

from pathlib import PurePosixPath

from newcode.permissions.normalizer import FILE_PATH_TOOLS
from newcode.permissions.types import (
    PermissionDecision,
    PermissionDecisionValue,
    PermissionLayer,
    PermissionRequest,
    RiskLevel,
)


SENSITIVE_FILE_NAMES = frozenset({"id_rsa", "id_ed25519"})
SENSITIVE_FILE_SUFFIXES = (".pem", ".key")
SENSITIVE_NAME_MARKERS = ("token", "secret", "credential")


def is_sensitive_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = PurePosixPath(normalized).name

    if name == ".env" or name.startswith(".env."):
        return True
    if name in SENSITIVE_FILE_NAMES:
        return True
    if name.endswith(SENSITIVE_FILE_SUFFIXES):
        return True
    return any(marker in normalized for marker in SENSITIVE_NAME_MARKERS)


def check_sensitive_file_policy(
    request: PermissionRequest,
) -> PermissionDecision | None:
    if request.tool_name not in FILE_PATH_TOOLS:
        return None

    path = _request_path(request)
    if path is None or not is_sensitive_path(path):
        return None

    return PermissionDecision(
        decision=PermissionDecisionValue.REQUIRE_CONFIRMATION,
        tool_name=request.tool_name,
        reason="Sensitive file access requires confirmation.",
        risk_level=RiskLevel.HIGH,
        matched_rule="built_in_sensitive_file",
        layer=PermissionLayer.BUILT_IN_RULES,
        original_args=request.original_args,
        normalized_args=request.normalized_args,
    )


def _request_path(request: PermissionRequest) -> str | None:
    for key in ("relative_path", "path", "resolved_path"):
        value = request.normalized_args.get(key)
        if isinstance(value, str):
            return value
    return None
