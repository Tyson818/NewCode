from __future__ import annotations

from .types import JsonObject, ToolContext, ToolFailure, ToolResult, ToolSpec, require_string
from .workspace import Workspace, is_skipped_path


DEFAULT_MAX_RESULTS = 100


class FindFilesTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="find_files",
            description=(
                "用于列出/查找工作区文件，按 glob 模式返回匹配文件。"
                "当用户要求列文件、查找文件、查看项目根目录文件时，优先使用该工具，"
                "而不是 shell 命令。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，例如 *.py 或 newcode/**/*.py"},
                    "max_results": {"type": "number", "description": "最多返回多少条结果"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        try:
            pattern = require_string(arguments, "pattern")
            max_results = _max_results(arguments)
            workspace = Workspace(context.workspace_root)
        except ToolFailure as exc:
            return ToolResult.failure(self.spec.name, exc.code, exc.message, exc.details)
        matches: list[str] = []
        for path in workspace.root.rglob(pattern):
            if len(matches) >= max_results:
                break
            if not workspace.contains_resolved(path):
                continue
            try:
                if path.is_file() and not is_skipped_path(path.relative_to(workspace.root)):
                    matches.append(workspace.relative(path))
            except (OSError, ValueError, ToolFailure):
                continue
        return ToolResult.success(
            self.spec.name,
            {"matches": matches, "truncated": len(matches) >= max_results},
        )


class SearchCodeTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_code",
            description="在工作区文本文件中搜索指定内容，返回文件、行号和匹配行。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要搜索的文本"},
                    "pattern": {"type": "string", "description": "可选 glob 文件模式，默认 *"},
                    "max_results": {"type": "number", "description": "最多返回多少条匹配"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        try:
            query = require_string(arguments, "query")
            pattern = arguments.get("pattern", "*")
            if not isinstance(pattern, str) or not pattern:
                pattern = "*"
            max_results = _max_results(arguments)
            workspace = Workspace(context.workspace_root)
        except ToolFailure as exc:
            return ToolResult.failure(self.spec.name, exc.code, exc.message, exc.details)
        matches: list[dict[str, object]] = []

        for path in workspace.root.rglob(pattern):
            if len(matches) >= max_results:
                break
            if not workspace.contains_resolved(path):
                continue
            if not path.is_file() or is_skipped_path(path.relative_to(workspace.root)):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if query in line:
                    matches.append(
                        {
                            "path": workspace.relative(path),
                            "line": line_number,
                            "text": line,
                        }
                    )
                    if len(matches) >= max_results:
                        break

        return ToolResult.success(
            self.spec.name,
            {"matches": matches, "truncated": len(matches) >= max_results},
        )


def _max_results(arguments: JsonObject) -> int:
    if "max_results" not in arguments:
        return DEFAULT_MAX_RESULTS
    value = arguments["max_results"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ToolFailure(
            "invalid_arguments",
            "参数 max_results 必须是正数",
            {"argument": "max_results"},
        )
    return max(1, min(int(value), DEFAULT_MAX_RESULTS))
