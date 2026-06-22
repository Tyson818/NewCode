from __future__ import annotations

from .types import JsonObject, ToolContext, ToolFailure, ToolResult, ToolSpec, require_string
from .workspace import Workspace


class ReadFileTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read_file",
            description="读取工作区内指定 UTF-8 文本文件的内容。",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "工作区相对文件路径"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        try:
            path_value = require_string(arguments, "path")
            workspace = Workspace(context.workspace_root)
            path = workspace.resolve_user_path(path_value)
            if not path.exists() or not path.is_file():
                return ToolResult.failure(
                    self.spec.name,
                    "file_not_found",
                    "文件不存在",
                    {"path": path_value},
                )
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult.failure(
                self.spec.name,
                "execution_error",
                "文件不是有效的 UTF-8 文本",
                {"path": path_value},
            )
        except ToolFailure as exc:
            return ToolResult.failure(self.spec.name, exc.code, exc.message, exc.details)
        return ToolResult.success(
            self.spec.name,
            {"path": workspace.relative(path), "content": content},
        )


class WriteFileTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="write_file",
            description="向工作区内指定文件写入 UTF-8 文本内容，必要时创建父目录。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区相对文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文件内容"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        try:
            path_value = require_string(arguments, "path")
            content = require_string(arguments, "content", allow_empty=True)
            workspace = Workspace(context.workspace_root)
            path = workspace.resolve_user_path(path_value)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except ToolFailure as exc:
            return ToolResult.failure(self.spec.name, exc.code, exc.message, exc.details)
        return ToolResult.success(
            self.spec.name,
            {"path": workspace.relative(path), "bytes": len(content.encode("utf-8"))},
        )


class ReplaceInFileTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="replace_in_file",
            description="在工作区文件中用新文本替换唯一匹配的原文。原文匹配零次或多次都会失败。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区相对文件路径"},
                    "old_text": {"type": "string", "description": "要替换的原文，必须恰好出现一次"},
                    "new_text": {"type": "string", "description": "替换后的新文本"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: JsonObject, context: ToolContext) -> ToolResult:
        try:
            path_value = require_string(arguments, "path")
            old_text = require_string(arguments, "old_text")
            new_text = require_string(arguments, "new_text", allow_empty=True)
            workspace = Workspace(context.workspace_root)
            path = workspace.resolve_user_path(path_value)
            if not path.exists() or not path.is_file():
                return ToolResult.failure(
                    self.spec.name,
                    "file_not_found",
                    "文件不存在",
                    {"path": path_value},
                )

            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult.failure(
                self.spec.name,
                "execution_error",
                "文件不是有效的 UTF-8 文本",
                {"path": path_value},
            )
        except ToolFailure as exc:
            return ToolResult.failure(self.spec.name, exc.code, exc.message, exc.details)
        count = content.count(old_text)
        if count != 1:
            return ToolResult.failure(
                self.spec.name,
                "not_unique_match",
                "原文必须恰好匹配一次",
                {"path": path_value, "matches": count},
            )

        path.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return ToolResult.success(
            self.spec.name,
            {"path": workspace.relative(path), "replacements": 1},
        )
