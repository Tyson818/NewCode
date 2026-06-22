# NewCode Tool System Tasks

## File List

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `newcode/session.py` | 扩展消息结构，支持工具调用和工具结果历史 |
| Modify | `newcode/providers/base.py` | 定义 Provider 事件和工具调用响应抽象 |
| Modify | `newcode/providers/deepseek.py` | 支持 tools 参数和流式工具调用解析 |
| Modify | `newcode/config.py` | 新增工具工作区和超时配置 |
| Modify | `newcode/cli.py` | 编排一次工具调用、工具结果回灌和最终回复 |
| Create | `newcode/tools/__init__.py` | 工具包导出 |
| Create | `newcode/tools/types.py` | 工具通用类型和接口 |
| Create | `newcode/tools/workspace.py` | 工作区路径解析和越界保护 |
| Create | `newcode/tools/registry.py` | 工具注册中心和默认工具集合 |
| Create | `newcode/tools/executor.py` | 工具执行、异常转换、敏感值遮蔽 |
| Create | `newcode/tools/file_tools.py` | 读文件、写文件、原文替换工具 |
| Create | `newcode/tools/search_tools.py` | 按模式找文件和搜代码内容 |
| Create | `newcode/tools/command_tool.py` | 非交互式命令执行工具 |
| Create | `tests/test_tools_file.py` | 文件工具测试 |
| Create | `tests/test_tools_search.py` | 搜索工具测试 |
| Create | `tests/test_tools_command.py` | 命令工具测试 |
| Create | `tests/test_tools_registry.py` | 注册中心和 schema 测试 |
| Create | `tests/test_tools_executor.py` | 执行器错误处理测试 |
| Create | `tests/test_provider_tool_calls.py` | Provider 流式工具调用解析测试 |
| Create | `tests/test_cli_tool_flow.py` | CLI 一次工具调用回灌测试 |

## T1: 定义工具系统通用类型

**Files:** `newcode/tools/types.py`, `newcode/tools/__init__.py`

**Depends On:** None

**Steps:**
1. 定义工具元信息、工具调用、工具结果、工具错误、工具上下文类型。
2. 定义统一工具协议，包含元信息和执行方法。
3. 确保工具结果可以稳定转换为 JSON 字符串。
4. 在工具包入口导出核心类型。

**Validation:** Run `python -m compileall newcode/tools`; expect tools package compiles without syntax errors.

## T2: 实现工作区路径保护

**Files:** `newcode/tools/workspace.py`, `tests/test_tools_executor.py`

**Depends On:** T1

**Steps:**
1. 实现工作区根目录解析。
2. 实现用户路径到真实路径的解析。
3. 阻止越过工作区根目录的路径。
4. 默认阻止访问 `.git` 内部路径。
5. 为正常路径、越界路径、`.git` 路径添加测试。

**Validation:** Run `python -m pytest tests/test_tools_executor.py -k "workspace or path"`; expect path safety tests pass.

## T3: 实现文件工具

**Files:** `newcode/tools/file_tools.py`, `tests/test_tools_file.py`

**Depends On:** T1, T2

**Steps:**
1. 实现 `read_file` 工具，读取工作区内 UTF-8 文本文件。
2. 实现 `write_file` 工具，创建父目录并写入 UTF-8 文本。
3. 实现 `replace_in_file` 工具，按原文唯一匹配替换。
4. 确保匹配零次或多次时不写文件并返回失败结果。
5. 添加读取成功、文件不存在、写入成功、替换成功、替换零次、替换多次、越界路径测试。

**Validation:** Run `python -m pytest tests/test_tools_file.py`; expect all file tool tests pass.

## T4: 实现搜索工具

**Files:** `newcode/tools/search_tools.py`, `tests/test_tools_search.py`

**Depends On:** T1, T2

**Steps:**
1. 实现 `find_files`，按 glob 模式返回工作区内匹配文件。
2. 实现 `search_code`，按文本查询返回文件、行号和匹配行。
3. 默认跳过 `.git`、`.venv`、`__pycache__` 等目录。
4. 限制返回结果数量，避免超大输出。
5. 添加有匹配、无匹配、目录跳过、结果限制测试。

**Validation:** Run `python -m pytest tests/test_tools_search.py`; expect all search tool tests pass.

## T5: 实现命令执行工具

**Files:** `newcode/tools/command_tool.py`, `tests/test_tools_command.py`

**Depends On:** T1, T2

**Steps:**
1. 实现非交互式命令执行，工作目录固定为项目根目录。
2. 捕获 exit code、stdout、stderr。
3. 支持默认超时和参数指定超时。
4. 超时返回结构化失败结果。
5. 非零退出码返回 `command_failed`，并保留 stdout/stderr。
6. 添加成功命令、失败命令、超时命令、空命令参数测试。

**Validation:** Run `python -m pytest tests/test_tools_command.py`; expect all command tool tests pass.

## T6: 实现工具注册中心

**Files:** `newcode/tools/registry.py`, `tests/test_tools_registry.py`

**Depends On:** T3, T4, T5

**Steps:**
1. 实现工具注册、按名查找和重复名称拒绝。
2. 实现工具列表转 OpenAI-compatible tools schema。
3. 实现默认注册中心，登记六个核心工具。
4. 测试六个工具名称存在。
5. 测试 schema 包含名称、描述和参数定义。

**Validation:** Run `python -m pytest tests/test_tools_registry.py`; expect registry and schema tests pass.

## T7: 实现统一工具执行器

**Files:** `newcode/tools/executor.py`, `tests/test_tools_executor.py`

**Depends On:** T1, T6

**Steps:**
1. 实现按工具名查找并执行工具调用。
2. 未知工具返回 `unknown_tool`。
3. 参数不合法返回 `invalid_arguments`。
4. 捕获工具异常并返回 `execution_error`。
5. 对结果中的敏感值做遮蔽。
6. 确保成功和失败结果都可 JSON 序列化。

**Validation:** Run `python -m pytest tests/test_tools_executor.py`; expect executor tests pass.

## T8: 扩展会话消息结构

**Files:** `newcode/session.py`, `tests/test_session.py`

**Depends On:** T1

**Steps:**
1. 扩展消息角色支持 `tool`。
2. 支持 assistant 工具调用消息。
3. 支持 tool 结果消息。
4. 保持原有 user / assistant 文本消息行为。
5. 更新 provider 消息转换格式。
6. 扩展现有会话测试，覆盖工具调用和工具结果。

**Validation:** Run `python -m pytest tests/test_session.py`; expect original and tool session tests pass.

## T9: 定义 Provider 事件模型

**Files:** `newcode/providers/base.py`

**Depends On:** T1, T8

**Steps:**
1. 定义文本增量事件。
2. 定义工具调用完成事件。
3. 更新 Provider 协议支持 tools 和是否允许工具调用。
4. 保持 ProviderError 行为不变。

**Validation:** Run `python -m compileall newcode/providers`; expect providers package compiles.

## T10: 实现 DeepSeek 流式工具调用解析

**Files:** `newcode/providers/deepseek.py`, `tests/test_provider_tool_calls.py`, `tests/test_deepseek_provider.py`

**Depends On:** T8, T9

**Steps:**
1. 请求模型时支持传入 tools schema。
2. 支持不允许工具调用的最终回复请求。
3. 解析普通 `delta.content` 为文本增量事件。
4. 解析并拼接 `delta.tool_calls` 中的名称和参数碎片。
5. 在流结束后输出完整工具调用事件。
6. JSON 参数解析失败时返回可识别错误事件。
7. 更新原有 Provider 测试适配事件模型。

**Validation:** Run `python -m pytest tests/test_provider_tool_calls.py tests/test_deepseek_provider.py`; expect provider tests pass without network requests.

## T11: 扩展配置加载

**Files:** `newcode/config.py`, `tests/test_config.py`

**Depends On:** T1

**Steps:**
1. 新增可选 `workspace_root` 配置。
2. 新增可选 `tool_timeout_seconds` 配置。
3. 新增可选 `command_timeout_seconds` 配置。
4. 为缺省值、合法值、非法类型和非法数值添加测试。
5. 保持 API Key 只从环境变量读取。

**Validation:** Run `python -m pytest tests/test_config.py`; expect config tests pass.

## T12: 实现 CLI 工具调用编排

**Files:** `newcode/cli.py`, `tests/test_cli_tool_flow.py`, `tests/test_cli.py`

**Depends On:** T6, T7, T8, T10, T11

**Steps:**
1. 启动时创建默认工具注册中心和工具上下文。
2. 用户输入后，第一次模型请求传入工具列表并允许工具调用。
3. 如果只收到文本增量，保持原流式输出和历史追加行为。
4. 如果收到一个工具调用，执行工具并追加 assistant tool call 和 tool result。
5. 工具结果回灌后发起第二次模型请求，禁止继续工具调用。
6. 如果收到多个工具调用，生成结构化失败工具结果并进入最终回复请求。
7. 如果最终回复继续请求工具，不继续执行，并给出清楚错误。
8. 保持 ProviderError 后可恢复。

**Validation:** Run `python -m pytest tests/test_cli_tool_flow.py tests/test_cli.py`; expect CLI tool flow and v0.1 CLI tests pass.

## T13: 增加越界与敏感信息回归测试

**Files:** `tests/test_tools_executor.py`, `tests/test_cli_tool_flow.py`

**Depends On:** T7, T12

**Steps:**
1. 测试工具结果不会包含配置的 API Key 明文。
2. 测试文件工具不能读取工作区外文件。
3. 测试命令工具输出中的敏感值被遮蔽。
4. 测试工具异常不会导致 CLI 崩溃。

**Validation:** Run `python -m pytest tests/test_tools_executor.py tests/test_cli_tool_flow.py -k "sensitive or outside or crash"`; expect safety regression tests pass.

## T14: 运行全量自动化验证

**Files:** `newcode/**`, `tests/**`

**Depends On:** T1-T13

**Steps:**
1. 运行 Python 语法编译检查。
2. 运行完整 pytest。
3. 确认测试不需要真实 DeepSeek API Key。
4. 如果 pytest 不可用，按项目规则记录原因并运行 unittest fallback。

**Validation:** Run `python -m compileall newcode` and `python -m pytest`; expect both pass.

## T15: 端到端手动验证

**Files:** runtime behavior

**Depends On:** T14

**Steps:**
1. 检查当前环境是否可用 tmux。
2. 如果可用，在 tmux 中启动 `python -m newcode`。
3. 输入一个读取项目文件的请求，观察是否触发工具并基于结果回复。
4. 输入一个修改文件请求，观察是否按唯一匹配策略处理。
5. 输入 `/exit`，确认干净退出。
6. 如果当前环境不能使用 tmux，记录原因并给出手动测试命令。

**Validation:** Run `tmux -V`; if available run the documented tmux scenario, otherwise provide manual commands and limitation reason.

## Execution Order

```text
T1
 |
T2 --------\
 |          \
T3           \
T4            -> T6 -> T7 ----\
T5 ---------/                  \
                                -> T12 -> T13 -> T14 -> T15
T8 -> T9 -> T10 ---------------/
 |
T11 ---------------------------/
```
