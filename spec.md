# Chapter 10：Command Registry & Dispatcher —— 统一 NewCode 的交互命令

## 背景与目标

NewCode 当前在 CLI 输入循环中以分支直接处理 `/plan`、`/do`、`/compact`、`/sessions` 和 `/resume`。本章建立固定、可测试的命令注册中心与分派器，统一命令发现、帮助、补全、状态输出和 AI 预设请求，同时保持已有会话、Context Management、Memory、MCP 与 Permission 的边界。命令不是工具，不是可扩展脚本系统，也不会绕过 AgentLoop。

## 功能需求

### F1：命令注册、元数据与启动校验

注册中心只包含程序内定义的内置命令。每项元数据至少包含规范名、别名、可见性、简短帮助、使用格式、命令类别、参数约束及模式/会话影响说明；名称均以 `/` 开头。

注册时以去除前导 `/` 后的 Unicode `casefold()` 形式比较规范名及别名。任意规范名或别名冲突时，启动以安全错误 `command_alias_conflict` 失败，绝不静默覆盖或按顺序择一。命令表不得由配置、会话、Prompt、MCP 或模型输出动态修改。

### F2：解析与未命中

首个非空字符为 `/` 时，按 Unicode 空白拆分命令名与参数；命令名大小写不敏感，参数原样保留在参数尾部。本章命令不支持 shell 展开、变量替换、管道、引号求值或文件读取。参数数量或格式不符时仅显示安全用法，不发起模型、工具、MCP 或文件操作。

未知 `/` 命令不送入 AI，输出安全“未知命令”提示及 `/help` 引导；普通非命令文本继续进入既有 AgentLoop，空白输入继续忽略。

### F3：命令类别与 UI control interface

命令只能属于：

- **纯本地**：读取受控本地状态或执行既有本地生命周期动作；不启动主模型回合、Agent 工具或 MCP。
- **界面状态**：只更新交互状态，例如 Plan/Do；不修改 Permission、Provider、会话历史或执行工具。
- **预设提示词送入 AI**：只产生固定、程序定义的用户请求，随后仍经既有 AgentLoop 主路径。

处理器只依赖独立 UI control interface，而不是 `print` 或具体终端框架。该接口呈现安全信息、错误、帮助、补全菜单和模式状态；CLI 是适配器。处理器返回声明式结果，分派器决定是否进入正常 AI 回合。

### F4：十个内置命令

| 命令 | 类别 | 参数与用途 | 输出与影响 |
|---|---|---|---|
| `/help` | 纯本地 | 可选命令名；显示可见命令或用法 | 不含隐藏命令、密钥、路径凭据或内部异常；不改状态。 |
| `/compact` | 纯本地控制 | 无参数；迁移现有手动 Context 压缩 | 保持既有压缩/无历史/失败、artifact sandbox、零工具摘要与熔断。 |
| `/clear` | 纯本地控制 | 无参数；checkpoint 当前会话后开始新受控 ChatSession | 清理旧 Context artifact；保留 Plan/Do、MemoryService、PermissionManager 与 MCP 生命周期。 |
| `/plan` | 界面状态 | 无参数；切为 Plan Mode | 保持 Plan Mode 工具可见性与零 MCP 调用边界。 |
| `/do` | 界面状态 | 无参数；切为 Do Mode | 保持既有 Permission、ToolScheduler 与 MCP adapter gate。 |
| `/session` | 纯本地 | `list`（默认）或 `resume <session-id>` | 迁移 `/sessions`、`/resume <id>`；仅当前 workspace 安全摘要，拒绝无效/过期/跨 workspace/不可恢复 ID。 |
| `/memory` | 纯本地 | 可选 `user`、`project`、`all`（默认） | 只显示受控、脱敏的记忆元数据/安全摘要；不调用 LLM、不写笔记。 |
| `/permission` | 纯本地 | 无参数 | 显示当前 mode、会话规则计数和安全诊断摘要；不显示规则原文或敏感参数，不改变授权。 |
| `/status` | 纯本地 | 无参数 | 显示 Plan/Do、session ID、上下文近似 usage/熔断安全状态、MemoryService 状态与 MCP 已知摘要；无网络 health check。 |
| `/review` | 预设提示词送入 AI | 无参数 | 固定请求：“审查当前工作区未提交变更，说明风险、证据和建议，不擅自修改。”；完整通过 AgentLoop 与既有 gate。 |

`/sessions` 是 `/session list` 的兼容别名，`/resume <id>` 是 `/session resume <id>` 的兼容别名；`/exit`、`/quit`、`exit` 保持 CLI 优先退出路径，不计入十个注册命令。

### F5：Tab 补全

补全只针对以 `/` 开头、尚未提交的命令名，使用与解析相同的大小写无关前缀匹配。隐藏命令永不参与候选、帮助或菜单。唯一匹配替换为规范名并保留参数；多个匹配不改输入，只经 UI control interface 显示按规范名排序的安全菜单；无匹配不显示候选。补全不运行命令、不请求 AI、不读写会话或文件。

### F6：兼容、安全与关闭

`/compact`、`/plan`、`/do`、`/sessions`、`/resume`、`/exit` 的可观察语义必须保持。命令层不得改变 Provider 配置、PromptBuilder 稳定模块、Permission 决策、ToolRegistry、ToolScheduler、MCP discovery/runtime/adapter、Context 压缩策略、Memory 自动写入策略或既有关闭顺序。

错误只使用稳定安全码或用户安全摘要；不得泄露 env、headers、secret、URL credential、完整异常、未脱敏会话内容、记忆正文或绝对路径。不得引入网络、真实第三方 MCP、生产 secret、tmux、RAG、向量数据库、自定义命令或动态生成的提示词。

## 非功能要求

- 注册、解析、补全和 handler 可用 fake UI、fake provider、临时 workspace/home 及既有依赖注入独立测试。
- 解析、注册和补全确定性；冲突、无效参数和 handler 失败不能破坏 CLI 循环。
- 不新增依赖；每 Phase 先 `python -m compileall newcode`，再跑 targeted pytest；最终跑全量 pytest 与 `git diff --check`。

## 不做的事项

- 自定义、插件化、配置驱动或运行时注册命令；
- 动态生成 `/review` 等命令提示词；
- 命令级 Permission、命令自身直接执行工具或绕过 AgentLoop；
- GUI/TUI 重构、网络 health check、MCP 协议扩展；
- 改变六个内置工具、Provider、MCP、Permission、Context 或 Memory 的安全模型。

## 验收标准

- AC1：十个内置命令及兼容别名可发现；重复规范名/别名启动安全失败。
- AC2：大小写不敏感解析、参数拒绝、未知 slash 的 `/help` 引导及普通文本进入 AgentLoop 有测试。
- AC3：三类命令只执行其允许动作，UI control interface 可用 fake 实现验证。
- AC4：既有 compact、Plan/Do、会话恢复、Context、Memory、MCP、Permission 和退出清理回归通过。
- AC5：补全排除隐藏命令；唯一替换、多匹配菜单、无匹配无副作用。
- AC6：`/clear`、`/session`、`/memory`、`/permission`、`/status`、`/review` 的成功、错误和脱敏覆盖通过。
- AC7：`/review` 走 AgentLoop，Plan/Do、Permission、ToolScheduler 与 MCP gate 未绕过。
- AC8：全量 `pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter10-final"`、compileall、diff check 与 fake CLI 验收通过。
