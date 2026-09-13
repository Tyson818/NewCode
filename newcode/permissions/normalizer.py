from __future__ import annotations

from pathlib import Path
from typing import Any

from newcode.permissions.types import JsonObject, PermissionMode, PermissionRequest


FILE_PATH_TOOLS = frozenset({"read_file", "write_file", "replace_in_file"})


def build_permission_request(
    tool_call: Any,
    context: Any,
    mode: PermissionMode,
    tool: Any | None = None,
) -> PermissionRequest:
    tool_name = getattr(tool_call, "name", "")
    arguments = getattr(tool_call, "arguments", {})
    original_args = dict(arguments) if isinstance(arguments, dict) else {}
    workspace_root = Path(getattr(context, "workspace_root", Path.cwd())).resolve()
    normalized_args: JsonObject = {
        "workspace_root": str(workspace_root),
    }

    if tool_name == "run_command":
        command = original_args.get("command")
        if isinstance(command, str):
            normalized_args["command"] = command

    if tool_name in FILE_PATH_TOOLS:
        path = original_args.get("path")
        if isinstance(path, str):
            normalized_args.update(_normalize_path(path, workspace_root))

    if tool_name == "find_files":
        pattern = original_args.get("pattern")
        if isinstance(pattern, str):
            normalized_args["pattern"] = pattern

    if tool_name == "search_code":
        query = original_args.get("query")
        if isinstance(query, str):
            normalized_args["query"] = query

    metadata = getattr(tool, "mcp_metadata", None)
    if isinstance(metadata, dict):
        for source_key, target_key in (("mcp_server", "mcp_server"), ("mcp_tool", "mcp_tool"), ("transport", "mcp_transport")):
            value = metadata.get(source_key)
            if isinstance(value, str):
                normalized_args[target_key] = value
        normalized_args["mcp_arguments"] = original_args

    return PermissionRequest(
        tool_name=tool_name,
        original_args=original_args,
        normalized_args=normalized_args,
        workspace_root=workspace_root,
        mode=mode,
    )


def _normalize_path(path: str, workspace_root: Path) -> JsonObject:
    raw_path = Path(path)
    candidate = raw_path if raw_path.is_absolute() else workspace_root / raw_path
    resolved_path = candidate.resolve(strict=False)
    normalized: JsonObject = {
        "path": path,
        "resolved_path": str(resolved_path),
    }

    try:
        normalized["relative_path"] = resolved_path.relative_to(workspace_root).as_posix()
    except ValueError:
        pass

    return normalized
