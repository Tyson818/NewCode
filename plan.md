# NewCode Tool System Plan

## Architecture Overview

NewCode v0.2 在现有 v0.1 对话闭环上增加工具系统，整体仍保持分层结构：

- CLI 层：维持终端输入循环、流式输出和错误展示。
- Session 层：扩展会话消息结构，支持 assistant 工具调用消息和 tool 结果消息。
- Provider 层：负责把工具定义传给模型，解析流式文本和流式工具调用参数。
- Tool 层：定义统一工具接口，提供六个核心工具。
- Registry 层：集中注册工具，按名称查找，并转换为 OpenAI-compatible tools 列表。
- Executor 层：统一执行工具，处理参数校验、超时、异常和结构化结果。
- Workspace 层：提供项目根目录内路径解析和越界保护。

每轮用户请求的数据流：

```text
用户输入
  -> CLI 追加 user 消息
  -> Provider 带 tools 发起流式请求
  -> 如果模型输出普通文本：CLI 流式打印并追加 assistant 消息
  -> 如果模型请求工具：Provider 拼接完整工具调用
  -> Executor 执行一个工具
  -> Session 追加 assistant tool_calls 消息和 tool 结果消息
  -> Provider 发起最终回复请求
  -> CLI 流式打印最终回复并追加 assistant 消息
  -> 回到下一轮输入
```

本阶段不做自动循环。最终回复请求不再允许模型继续调用工具。

## Core Data Structures

### ChatMessage

表示当前会话中的一条消息。

字段：

- `role: str`：支持 `user`、`assistant`、`tool`
- `content: str | None`：普通文本内容，assistant 工具调用消息可以为空
- `tool_calls: list[ToolCall] | None`：assistant 请求工具时携带
- `tool_call_id: str | None`：tool 结果消息对应的工具调用 ID

### ToolCall

表示模型请求执行的一个工具调用。

字段：

- `id: str`：模型生成的工具调用 ID
- `name: str`：工具名称
- `arguments: dict[str, object]`：已解析后的 JSON 参数
- `raw_arguments: str`：原始 JSON 参数字符串，解析失败时用于错误报告

### ToolSpec

表示一个工具对模型公开的元信息。

字段：

- `name: str`
- `description: str`
- `parameters: dict[str, object]`

### ToolResult

表示本地工具执行结果。

字段：

- `ok: bool`
- `tool_name: str`
- `data: object | None`
- `error: ToolError | None`
- `metadata: dict[str, object]`

### ToolError

表示工具失败详情。

字段：

- `code: str`
- `message: str`
- `details: dict[str, object]`

常见错误码：

- `invalid_arguments`
- `unknown_tool`
- `path_not_allowed`
- `file_not_found`
- `not_unique_match`
- `timeout`
- `command_failed`
- `execution_error`
- `multiple_tool_calls_not_supported`
- `tool_call_parse_error`

### ToolContext

工具执行上下文。

字段：

- `workspace_root: Path`
- `default_timeout_seconds: float`
- `command_timeout_seconds: float`
- `sensitive_values: list[str]`

## Core Interfaces

### Tool

```python
class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec:
        ...

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolResult:
        ...
```

### ToolRegistry

```python
class ToolRegistry:
    def register(self, tool: Tool) -> None:
        ...

    def get(self, name: str) -> Tool | None:
        ...

    def to_openai_tools(self) -> list[dict[str, object]]:
        ...
```

## Module Design

### `newcode.session`

**Responsibility:** 管理多角色会话历史。

**Public Interface:**

- `ChatMessage`
- `ToolCall`
- `ChatSession`
- `ChatSession.add_user_message(content: str) -> None`
- `ChatSession.add_assistant_message(content: str) -> None`
- `ChatSession.add_assistant_tool_call(tool_call: ToolCall) -> None`
- `ChatSession.add_tool_result(tool_call_id: str, result: ToolResult) -> None`
- `ChatSession.to_provider_messages() -> list[dict[str, object]]`

### `newcode.providers.base`

**Responsibility:** 定义 Provider 抽象、Provider 错误和事件模型。

**Public Interface:**

- `ChatProvider`
- `ProviderError`
- `ProviderEvent`
- `TextDelta`
- `ToolCallEvent`

### `newcode.providers.deepseek`

**Responsibility:** 使用 OpenAI SDK 调 DeepSeek，并支持工具定义与流式工具调用解析。

### `newcode.tools.types`

**Responsibility:** 定义工具系统通用类型。

### `newcode.tools.registry`

**Responsibility:** 工具集中注册和 API tools 转换。

默认注册六个工具：

- `read_file`
- `write_file`
- `replace_in_file`
- `run_command`
- `find_files`
- `search_code`

### `newcode.tools.executor`

**Responsibility:** 统一执行工具调用并返回结构化结果。

### `newcode.tools.workspace`

**Responsibility:** 工作区路径解析和越界保护。

### `newcode.tools.file_tools`

**Responsibility:** 文件读取、写入、替换。

### `newcode.tools.search_tools`

**Responsibility:** 文件模式查找和代码内容搜索。

### `newcode.tools.command_tool`

**Responsibility:** 执行非交互式本地命令。

### `newcode.cli`

**Responsibility:** 编排用户输入、模型请求、工具执行和最终回复。

### `newcode.config`

**Responsibility:** 保持现有配置能力，新增工具相关可选配置。

新增可选配置：

- `workspace_root`
- `tool_timeout_seconds`
- `command_timeout_seconds`

## Module Interaction

### Startup

```text
python -m newcode
  -> cli.main
  -> load_config
  -> resolve_api_key
  -> DeepSeekProvider
  -> ChatSession
  -> create_default_registry
  -> ToolContext
  -> run_conversation
```

### Text-Only Turn

```text
user input
  -> session.add_user_message
  -> provider.stream_chat(messages, tools, allow_tool_calls=True)
  -> TextDelta events
  -> CLI prints chunks
  -> session.add_assistant_message
```

### Tool Turn

```text
user input
  -> session.add_user_message
  -> provider.stream_chat(messages, tools, allow_tool_calls=True)
  -> ToolCall event
  -> validate exactly one tool call
  -> execute_tool_call
  -> session.add_assistant_tool_call
  -> session.add_tool_result
  -> provider.stream_chat(messages, tools=None, allow_tool_calls=False)
  -> TextDelta events
  -> CLI prints final answer
  -> session.add_assistant_message
```

## File Organization

```text
newcode/
+-- cli.py
+-- config.py
+-- session.py
+-- providers/
|   +-- base.py
|   +-- deepseek.py
+-- tools/
|   +-- __init__.py
|   +-- types.py
|   +-- registry.py
|   +-- executor.py
|   +-- workspace.py
|   +-- file_tools.py
|   +-- search_tools.py
|   +-- command_tool.py
tests/
+-- test_tools_file.py
+-- test_tools_search.py
+-- test_tools_command.py
+-- test_tools_registry.py
+-- test_tools_executor.py
+-- test_provider_tool_calls.py
+-- test_cli_tool_flow.py
```

## Technical Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| 工具接口 | 每个工具暴露 `spec` 和 `run(arguments, context)` | 简单统一，便于注册、测试和后续扩展 |
| 工具 schema | 使用 OpenAI-compatible function tools JSON Schema | 贴合 DeepSeek / OpenAI-compatible Chat Completions |
| 工具调用解析 | 在 Provider 层拼接流式 `tool_calls` 参数碎片 | 隔离 API 细节，CLI 只处理完整工具调用 |
| 工具结果 | 成功和失败都返回 `ToolResult` | 模型可基于失败原因调整回复，程序不崩溃 |
| 文件范围 | 默认限制在工作区根目录内 | 避免工具任意读写系统文件 |
| 改文件方式 | 原文唯一匹配替换 | 可预测、易测试，失败时不会误改多处 |
| 搜索实现 | 先用 Python 标准库 | 避免新增依赖，符合当前项目轻量原则 |
| 命令执行 | 非交互式、固定工作区、捕获输出、带超时 | 满足基础能力，同时避免长期阻塞 |
| 多工具调用 | 本阶段拒绝并回传结构化失败 | 严格遵守 spec，不提前实现 Agent Loop |
| 最终回复请求 | 工具结果回灌后禁用工具 | 防止模型继续请求工具导致隐式循环 |

## Requirements Coverage

| Requirement | Architectural Owner |
|-------------|---------------------|
| F1 | `newcode.cli`, `newcode.providers.deepseek` |
| F2 | `newcode.providers.deepseek`, `newcode.providers.base` |
| F3 | `newcode.tools.registry`, `newcode.providers.deepseek` |
| F4 | `newcode.tools.file_tools` |
| F5 | `newcode.tools.file_tools` |
| F6 | `newcode.tools.file_tools` |
| F7 | `newcode.tools.search_tools` |
| F8 | `newcode.tools.search_tools` |
| F9 | `newcode.tools.command_tool` |
| F10 | `newcode.tools.executor`, `newcode.tools.types` |
| F11 | `newcode.providers.deepseek` |
| F12 | `newcode.cli` |
| F13 | `newcode.cli`, `newcode.tools.executor` |
| F14 | `newcode.session` |
| F15 | `newcode.tools.executor`, `newcode.config` |
