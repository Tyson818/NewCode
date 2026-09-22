# Chapter 10：Command Registry & Dispatcher 计划

## 架构总览

输入路径为：`CLI input -> exit 优先判断 -> CommandParser -> CommandRegistry/Dispatcher -> CommandOutcome -> UIControl 或既有 AgentLoop`。非 `/` 文本不经过命令注册中心，仍保持原 AgentLoop 路径；未知 `/` 文本停在分派器，绝不送模型。

```text
CommandDefinition -> CommandRegistry -> CommandDispatcher
                                      |       |       |
                                   local    UI      fixed AI input
                                      |       |       |
                                  UIControl UIControl existing AgentLoop
```

## 核心接口

### CommandDefinition 与结果

`CommandDefinition` 为不可变静态元数据：`name`、`aliases`、`visibility`、`category`、`summary`、`usage`、`argument_spec`、`handler`。名称经去 `/` 与 `casefold()` 规范化。

`ParsedCommand` 保存原始输入、规范命令、参数 token 与参数尾部。`CommandOutcome` 只能表达 `handled`、`ai_input`、`mode_change`、`session_replaced`、`error`，不携带工具调用、Provider 或文件句柄。

### UIControl

`UIControl` 协议定义 `info(text)`、`error(code, text)`、`help(entries)`、`completion_menu(entries)`、`set_mode(mode)`。CLI adapter 映射到既有 `output/error_output`；fake UI 收集调用。handler 不直接 `print`。

### CommandRuntime

分派器取得窄 `CommandRuntime`：当前 mode/session、ContextManager、SessionArchive、MemoryStore/MemoryService、PermissionManager、MCP 安全摘要、workspace。它只暴露已存在的安全查询及受控生命周期动作；不暴露 Provider、ToolRegistry、ToolScheduler 或 MCP client。`/review` 只返回固定 AI 输入，CLI 后续照常创建 AgentLoop。

## 模块与文件

| 文件 | 责任 |
|---|---|
| `newcode/commands/__init__.py` | 导出公共模型与默认注册表。 |
| `newcode/commands/types.py` | 元数据、类别、解析、outcome、安全错误码。 |
| `newcode/commands/registry.py` | 静态注册、规范化、冲突、帮助、补全候选。 |
| `newcode/commands/dispatcher.py` | 输入解析、参数校验、handler 调用、未知引导。 |
| `newcode/commands/builtins.py` | 十个内置命令和兼容别名。 |
| `newcode/commands/ui.py` | UIControl 协议与 CLI adapter。 |
| `newcode/cli.py` | 用 dispatcher 替换命令分支；保留输入循环、AgentLoop、finally。 |
| `tests/test_commands_*.py` | 注册、解析、补全、UI、内置命令单测。 |
| `tests/test_cli_commands.py` | CLI 命令及既有 gate 集成回归。 |

原则上不修改 Provider、PromptBuilder、PermissionManager、ToolRegistry、ToolScheduler、MCP runtime/manager/adapter、Context 或 Memory 核心算法。测试直接证明缺少安全读取 accessor 时，才最小扩展。

## 内置命令实现策略

- `/help` 读取 registry 可见元数据，支持规范名或别名。
- `/compact` 调用现有 `ContextManager.manual_compact`，复用已有零工具摘要 generator。
- `/clear` 先 checkpoint、再 Context cleanup、再建新 `ChatSession`/archive/ContextManager；任何失败保留旧归档。
- `/plan`、`/do` 只返回 mode outcome，CLI 更新 mode，不触及 Permission mode。
- `/session` 调用 `SessionArchive.list_recoverable/restore`；兼容旧别名映射同一 handler。
- `/memory` 使用 MemoryStore 的受控读取/选择接口，仅输出脱敏 metadata。
- `/permission` 汇总 PermissionManager 的 mode、会话规则数和规则加载错误计数。
- `/status` 汇总 CLI 已知状态、Context usage/熔断、MemoryService 状态和 MCP 已知 discovery 摘要；绝不主动探测。
- `/review` 无参数时产生固定请求，后续走正常 AgentLoop；Plan/Do、Permission、ToolScheduler、MCP 仍在原位置生效。

## Phase

### Phase 1：注册、解析与 UI 边界

目标：命令模型、静态注册、冲突失败、大小写解析、帮助、补全、fake UI。文件：`newcode/commands/*`、`tests/test_commands_registry.py`、`tests/test_commands_dispatcher.py`、`tests/test_commands_ui.py`。完成：普通文本/未知 slash/参数错误稳定区分，隐藏命令不泄露。

### Phase 2：内置命令与 CLI 迁移

目标：十个处理器、兼容别名、CLI 接入。文件：`newcode/commands/builtins.py`、`newcode/cli.py`、`tests/test_commands_builtins.py`、`tests/test_cli_commands.py`及必要最小状态 accessor。完成：旧命令、退出、checkpoint、cleanup 顺序不变。

### Phase 3：安全 gate、补全与端到端

目标：验证 `/review`、Plan/Do、Permission、MCP、Context、Memory、session 及补全无副作用。文件：Phase 2 测试与必要最小回归。完成：fake provider/local fixture 证据完整。

### Phase 4：全量验收

目标：静态边界审计、全量测试、受控 CLI 人工验收。文件：仅失败直接证明的最小模块与测试。完成：无 tmux、网络、secret、第三方 MCP。

## 技术决定

| 决定 | 选择 | 原因 |
|---|---|---|
| 名称 | 去 `/` 后 `casefold()` | 一致的大小写无关查找及冲突检测。 |
| 参数 | 非 shell 空白 token，不求值 | 十项命令无需路径表达式，安全且跨平台。 |
| 未知 slash | 本地 `/help` 引导 | 避免控制输入进入模型。 |
| 兼容 | `/sessions`、`/resume` 作为别名 | 保持 Chapter 9 可观察行为。 |
| UI | 窄协议 + CLI adapter | 单测不绑定终端框架。 |
| review | 固定字符串经 AgentLoop | 无动态 prompt，保留所有 gate。 |
| 状态 | 仅已有状态快照 | 避免新网络生命周期。 |

## 验证约定

每 Phase 先：

```powershell
python -m compileall newcode
python -m pytest <targeted tests> -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter10-phaseN"
```

最终：

```powershell
python -m compileall newcode
python -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter10-final"
git diff --check
```
