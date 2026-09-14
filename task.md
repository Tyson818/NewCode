# Chapter 8：Context Management 任务拆分

## 全局执行约束

- 仅在每个 Phase 的前置 targeted pytest 和 `python -m compileall newcode` 通过后进入下一 Phase。
- 所有 pytest 使用系统 TEMP 的独立 `--basetemp`；不用仓库目录作为临时目录。
- 摘要模型请求必须显式零工具调用，且不得递归触发 Context Management 压缩；一次自动或手动摘要请求最多尝试一次。
- artifact 只允许 Context Management 在 workspace sandbox 内的 `.newcode/context-artifacts/<session-id>/` 写入；它不是 Agent 工具调用，禁止任意其他写入路径。
- 近期保留区用户消息必须逐字保留；不得为压缩、预览或摘要改写它们。
- 连续 3 次摘要失败打开熔断；熔断后关闭自动摘要，`/compact` 仅可显式单次重试。
- Provider 不得导入或理解 Context Management；Context Management 不得调用 Agent 工具、MCP、PermissionManager、ToolScheduler 或网络。
- 不实现精确 tokenizer、自动窗口探测、向量记忆、云同步、真实网络或机器学习式摘要优化。

## Phase 1：会话版本、近似 token 与 usage 锚点

### T1：建立 Context Management 类型与会话版本

**前置条件：** Chapter 8 的 `spec.md`、`plan.md` 已批准。

**允许修改/新增：**

- 新增 `newcode/context/__init__.py`、`newcode/context/types.py`
- 修改 `newcode/session.py`
- 新增 `tests/test_context_estimator.py`
- 必要时仅修改 `tests/test_session.py`

**实现动作：**

1. 定义 session id、会话版本、usage 锚点、估算和压缩状态的领域类型。
2. 为 ChatSession 增加受控的版本读取/替换能力；任何压缩性替换递增版本，普通既有消息追加语义不变。
3. 保持消息顺序、tool-call/result 配对和用户消息原文不变。

**对应测试：** 会话版本单调递增、追加回归、替换后顺序/配对不变。

**完成判定：** 无 Provider、文件系统或工具调用时可验证类型与 session 版本语义。

**不得跨越的边界：** 不实现估算、artifact、摘要或 AgentLoop 接入。

### T2：实现稳定近似估算与 usage 锚点

**前置条件：** T1 通过。

**允许修改/新增：**

- 新增 `newcode/context/estimator.py`
- 修改 `newcode/context/types.py`、`newcode/context/__init__.py`
- 修改 `tests/test_context_estimator.py`

**实现动作：**

1. 按稳定 JSON、`ceil(chars / 2)`、消息 12、tool 24 和 1.20 系数实现全量估算。
2. 实现可信 prompt/input usage 锚点及锚点后增量估算。
3. 会话版本变化时使锚点失效；非法、缺失、负数 usage 与摘要请求 usage 不得覆盖锚点。

**对应测试：** Unicode、结构化消息、tool call/result、边界相等、稳定性、锚点增量、失效和非法 usage。

**完成判定：** 相同输入产生相同估算，且可独立证明锚点不会跨压缩性会话版本使用。

**不得跨越的边界：** 不引入 tokenizer 依赖、模型窗口探测或 Provider 修改。

**Phase 验证：**

```powershell
.\.venv\Scripts\python.exe -m compileall newcode
.\.venv\Scripts\python.exe -m pytest tests/test_context_estimator.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase1"
```

## Phase 2：敏感信息遮蔽与安全 artifact 存储

### T3：实现 Context Management 脱敏

**前置条件：** Phase 1 通过。

**允许修改/新增：**

- 新增 `newcode/context/redaction.py`、`tests/test_context_redaction.py`
- 修改 `newcode/context/types.py`

**实现动作：**

1. 复用既有敏感值集合，并递归遮蔽敏感字段名及常见变体。
2. 将相同规则用于预览、artifact、摘要、边界消息和安全诊断。
3. 不可安全序列化/遮蔽的值只产生安全占位说明。

**对应测试：** 嵌套 token/secret/password/credential/authorization/cookie/api key、headers、env、URL credential、异常文本和已知值均不泄露。

**完成判定：** 所有 Context Management 输出均不含 fixture secret。

**不得跨越的边界：** 不改写会话里的用户原始消息，不写文件，不接入工具或 CLI。

### T4：实现 sandbox 内 artifact 写入、截断与清理

**前置条件：** T3 通过。

**允许修改/新增：**

- 新增 `newcode/context/artifacts.py`、`tests/test_context_artifacts.py`
- 修改 `newcode/context/types.py`、`.gitignore`

**实现动作：**

1. 解析 workspace root，只允许 `.newcode/context-artifacts/<safe-session-id>/` 为写入根。
2. 以内部序号/生成标识命名 JSON-safe、已脱敏记录，限制 20 MiB 并记录截断状态。
3. 拒绝绝对路径、`..`、恶意 session id、符号链接逃逸及 sandbox 外目标。
4. 支持当前会话清理和专用根内超过 7 天的陈旧会话清理；写入失败不生成虚假路径。

**对应测试：** 正常写入、20 MiB 截断、相对路径、写入失败、JSON-safe 失败、路径逃逸、零 Agent 工具调用、清理范围。

**完成判定：** artifact 唯一位于 workspace sandbox 内，且 `.gitignore` 忽略运行时 artifact 根。

**不得跨越的边界：** 不向 TEMP、用户目录或模型指定路径写入；不接入 AgentLoop、摘要或 CLI。

**Phase 验证：**

```powershell
.\.venv\Scripts\python.exe -m compileall newcode
.\.venv\Scripts\python.exe -m pytest tests/test_context_redaction.py tests/test_context_artifacts.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase2"
```

## Phase 3：第一层工具结果外置

### T5：实现单结果阈值与安全预览

**前置条件：** Phase 2 通过。

**允许修改/新增：**

- 新增 `newcode/context/prevention.py`、`tests/test_context_prevention.py`
- 修改 `newcode/context/types.py`、`newcode/session.py`

**实现动作：**

1. 对工具结果按 Phase 1 估算；单结果 `>= 8,000 token` 时写入 artifact。
2. 仅以最多 1,200 个已脱敏字符的预览、相对路径和截断/省略状态替换工具结果表示。
3. 外置成功才替换消息并失效 usage 锚点；失败保留原结果及安全诊断。

**对应测试：** 8K 等值/临界值、预览长度、脱敏、失败原子性、用户原文与 tool-result 配对不变。

**完成判定：** 单工具结果的外置幂等，且不修改任何非工具内容。

**不得跨越的边界：** 不调用 SummaryGenerator、Provider 或 Agent 工具。

### T6：实现单消息累计阈值与稳定选择顺序

**前置条件：** T5 通过。

**允许修改/新增：**

- 修改 `newcode/context/prevention.py`、`tests/test_context_prevention.py`
- 必要时修改 `newcode/context/types.py`

**实现动作：**

1. 单消息工具结果累计 `>= 12,000 token` 时，按估算值降序外置。
2. 同值按原始 tool-call 顺序；已外置结果跳过，直至消息低于阈值。
3. 保持所有用户消息、assistant 非工具文本、tool-call id 和结果顺序不变。

**对应测试：** 多结果排序、同值稳定性、12K 等值、重复预处理幂等和未外置结果保留。

**完成判定：** 选择顺序可复现，且不产生第二套 artifact/脱敏路径。

**不得跨越的边界：** 不发起第二层摘要，不修改 Prompt 或 CLI。

**Phase 验证：**

```powershell
.\.venv\Scripts\python.exe -m compileall newcode
.\.venv\Scripts\python.exe -m pytest tests/test_context_prevention.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase3"
```

## Phase 4：第二层摘要、边界消息与熔断

### T7：实现近期保留选择与摘要输入

**前置条件：** Phase 3 通过。

**允许修改/新增：**

- 新增 `newcode/context/history.py`、`newcode/context/summary.py`
- 新增 `tests/test_context_history.py`、`tests/test_context_summary.py`
- 修改 `newcode/context/types.py`

**实现动作：**

1. 自动阈值为 51K；从尾部保留约 10K token 与至少 5 条消息所需的较大范围。
2. 保留完整 tool-call/result 交换、系统约束、边界消息及近期保留区内用户消息的完整原文。
3. 较早用户消息可摘要替换，但仍有效约束须尽量逐字摘录并标记“原文摘录”或“归纳”；已有摘要参与再摘要输入。

**对应测试：** 51K 触发、无可压缩历史、尾部边界、完整交换、近期用户原文、旧摘要再摘要。

**完成判定：** 可压缩区与保留区选择稳定，且不改写近期用户消息。

**不得跨越的边界：** 不调用真实 Provider、网络、文件读取工具或 CLI。

### T8：实现零工具摘要格式、草稿丢弃与边界消息

**前置条件：** T7 通过。

**允许修改/新增：**

- 修改 `newcode/context/summary.py`、`newcode/context/history.py`
- 修改 `tests/test_context_summary.py`、`tests/test_context_history.py`

**实现动作：**

1. 定义注入式 SummaryGenerator；摘要请求显式空工具集合，且跳过 ContextManager 的预处理入口，绝不递归压缩。
2. 生成要求“先草稿、后正式摘要”的固定 prompt，并要求响应严格使用且仅使用以下可解析包络：`<analysis_draft>...</analysis_draft>` 后接 `<structured_summary>...</structured_summary>`。
3. 解析器只提取、保存并校验 `structured_summary` 的内容；`analysis_draft` 与原始完整响应不得写入 session、artifact、日志或 CLI。
4. 缺失任一包络、同一包络重复、包络顺序错误、两个包络之外存在非空内容，或 `structured_summary` 缺少七个固定章节、证据/推断/未确认标识时，均视为摘要失败，且会话保持不变。
5. 成功时以已校验的 `structured_summary` 替换早期历史并插入要求重新读取 artifact 的安全边界消息。

**对应测试：** generator 接收零工具、每次请求最多一次尝试；正确双包络的 `structured_summary` 被保存；草稿和原始完整响应不存储；缺失包络、重复包络、错误顺序、包络外非空内容、七段缺失均失败；边界消息、安全索引和失败原子性。

**完成判定：** fake generator 可证明不存在 tool call、文件读取或递归 prepare。

**不得跨越的边界：** 不让摘要模型执行工具、读取 artifact 或使用主请求的工具许可。

### T9：实现连续失败熔断与单次手动重试

**前置条件：** T8 通过。

**允许修改/新增：**

- 新增 `newcode/context/manager.py`
- 修改 `newcode/context/types.py`、`newcode/context/history.py`
- 新增 `tests/test_context_manager.py`

**实现动作：**

1. 连续 3 次摘要失败后打开熔断，自动请求不再尝试摘要。
2. 每次自动或手动压缩调用最多发起一次摘要请求；失败不改变历史。
3. `/compact` 的未来入口可显式请求一轮单次重试；成功关闭熔断并归零失败计数，失败维持打开。

**对应测试：** 三次失败、自动禁用、成功重置、熔断后手动单次重试、异常/格式失败统一计数。

**完成判定：** 无自动重试循环，熔断与会话状态跨多次请求可观察。

**不得跨越的边界：** 不实现 CLI 命令，不调用真实 API 或工具。

**Phase 验证：**

```powershell
.\.venv\Scripts\python.exe -m compileall newcode
.\.venv\Scripts\python.exe -m pytest tests/test_context_history.py tests/test_context_summary.py tests/test_context_manager.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase4"
```

## Phase 5：AgentLoop 请求前接入

### T10：接入请求前顺序与 usage 回写

**前置条件：** Phase 4 通过。

**允许修改/新增：**

- 修改 `newcode/agent/loop.py`
- 仅必要时修改 `newcode/agent/collector.py`、`newcode/agent/events.py`
- 修改 `newcode/context/manager.py`、`newcode/context/__init__.py`
- 新增 `tests/test_agent_loop_context.py`
- 修改 `tests/test_context_manager.py`、必要时 `tests/test_agent_loop.py`

**实现动作：**

1. 每次 `stream_chat` 前固定执行“新增消息/工具结果 → 第一层 → 估算 → 至多一次第二层 → 构造主请求”。
2. 将窄 SummaryGenerator 回调从 AgentLoop 注入 ContextManager；摘要调用零工具、不递归 prepare。
3. 仅在主请求成功返回可信 prompt/input usage 后记录锚点。
4. 让一个 ChatSession 跨多轮 AgentLoop 复用同一 ContextManager。

**对应测试：** 多轮/工具循环顺序、usage 回写、自动摘要最多一次、摘要零工具、主请求保留既有 tools、取消与 Provider error 回归。

**完成判定：** 所有压缩均发生在主 Provider 请求前，Provider 无 Context Management 导入。

**不得跨越的边界：** 不让 ContextManager 执行工具、权限确认、MCP；不改变 Plan/Do、Permission、ToolScheduler 或工具排序。

### T11：验证端到端工具流与边界不绕过

**前置条件：** T10 通过。

**允许修改/新增：**

- 修改 `tests/test_agent_loop_context.py`
- 必要时修改 `tests/test_agent_loop.py`、`tests/test_agent_loop_mcp.py`

**实现动作：**

1. 用 fake provider 和本地 fixture 验证大工具结果外置后，模型仅见预览/路径。
2. 验证边界消息令模型经既有 read_file 链路重新读取，而非从摘要推断细节。
3. 覆盖 Plan/Do、Permission、MCP 和 scheduler 的既有 gate 仍生效。

**对应测试：** 外置→重新读取→最终回答流；Plan Mode、permission deny、MCP deny、串行调度和六内置工具回归。

**完成判定：** Context Management 未绕过任何现有执行链路。

**不得跨越的边界：** 不测试真实网络、生产 secret 或第三方 MCP server。

**Phase 验证：**

```powershell
.\.venv\Scripts\python.exe -m compileall newcode
.\.venv\Scripts\python.exe -m pytest tests/test_context_manager.py tests/test_agent_loop_context.py tests/test_agent_loop.py tests/test_agent_loop_mcp.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase5"
```

## Phase 6：CLI `/compact` 与 artifact 生命周期

### T12：增加会话级 ContextManager 与 `/compact`

**前置条件：** Phase 5 通过。

**允许修改/新增：**

- 修改 `newcode/cli.py`
- 修改 `newcode/context/manager.py`
- 新增 `tests/test_cli_context.py`
- 必要时修改 `tests/test_cli_tool_flow.py`

**实现动作：**

1. 由 `run_conversation` 为同一 ChatSession 创建、复用 ContextManager，并绑定 workspace/sensitive values。
2. 增加 `/compact`：先运行第一层；只要存在可压缩历史，强制进行一次摘要。
3. 61K 只作为安全状态和建议显示，绝不阻止已请求的手动强制压缩。
4. 无历史、摘要失败和熔断状态仅输出脱敏摘要，不输出 prompt、artifact 内容、headers、env 或异常堆栈。

**对应测试：** 强制优先级、61K 状态、无历史、成功、失败、熔断后手动重试、既有 `/plan`/`/do`/`/exit` 回归。

**完成判定：** CLI 只委托 ContextManager，不重复实现摘要/存储逻辑。

**不得跨越的边界：** 不改变 MCP lifecycle、Permission confirmer、Provider 配置或既有命令含义。

### T13：在所有 CLI 退出路径清理 artifact

**前置条件：** T12 通过。

**允许修改/新增：**

- 修改 `newcode/cli.py`
- 修改 `tests/test_cli_context.py`

**实现动作：**

1. 在当前 CLI 生命周期的 finally 中清理当前会话 artifact，保留既有 MCP shutdown 顺序与幂等性。
2. 覆盖 EOF、`/exit`、KeyboardInterrupt、AgentLoop 异常和启动后异常。
3. 清理失败仅报告安全诊断，不影响其他 cleanup，且绝不清理 sandbox 外文件。

**对应测试：** 每个退出路径、清理失败隔离、当前 session 目录清理、其他 session/工作区文件保留、CLI 输出脱敏。

**完成判定：** 退出后不遗留当前会话 artifact，且现有 CLI 行为不变。

**不得跨越的边界：** 不安装 tmux，不使用真实生产 secret 或真实网络。

**Phase 验证：**

```powershell
.\.venv\Scripts\python.exe -m compileall newcode
.\.venv\Scripts\python.exe -m pytest tests/test_cli_context.py tests/test_cli_tool_flow.py tests/test_cli_mcp.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase6"
```

## Phase 7：全量回归与人工验收

### T14：执行全量自动回归与边界审计

**前置条件：** Phase 1–6 全部 targeted pytest 通过。

**允许修改/新增：**

- 原则上不修改生产文件。
- 仅允许修复失败直接证明的最小模块，并同步补最小回归测试。

**实现动作：**

1. 审计 Provider 不导入 Context Management，Context Management 不导入工具/MCP/权限执行层。
2. 审计 artifact 仅在 sandbox 专用目录、摘要零工具且不递归、近期用户原文未改写。
3. 运行全部自动回归与差异检查。

**对应测试：** 全部 Chapter 8 测试及 Agent、CLI、Prompt、Permission、MCP、Provider 回归。

**完成判定：** 全量测试通过；任何 skipped 项都有可复现环境原因；无范围外能力。

**不得跨越的边界：** 不以人工验收替代测试，不为通过测试重构无关模块。

### T15：执行受控人工验收

**前置条件：** T14 通过。

**允许修改/新增：** 无；若发现问题，先新增最小自动化回归测试，再修复最小缺陷。

**实现动作：**

1. 使用 fake provider/local fixture 验收普通会话、超大工具结果、自动摘要、`/compact`、三次失败熔断、退出清理和敏感输出。
2. 验证模型需要细节时通过既有读取工具访问 artifact，而非按摘要臆测。
3. 记录未使用真实网络、生产 secret 或第三方 MCP server 的证据。

**对应测试：** 人工本地 CLI 流与 T14 全量自动回归结果。

**完成判定：** 所有验收场景有可观察证据，当前会话 artifact 在退出后已清理。

**不得跨越的边界：** 不安装 tmux；若环境无 tmux，报告原因并以自动化 CLI fixture 验收为依据。

**Phase 验证：**

```powershell
.\.venv\Scripts\python.exe -m compileall newcode
.\.venv\Scripts\python.exe -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-context-final"
git diff --check
```

## 执行顺序

```text
Phase 1: T1 -> T2
Phase 2: T3 -> T4
Phase 3: T5 -> T6
Phase 4: T7 -> T8 -> T9
Phase 5: T10 -> T11
Phase 6: T12 -> T13
Phase 7: T14 -> T15
```

任一任务出现测试失败、需要未列出的生产文件、无法保持摘要零工具/不递归边界、artifact sandbox 无法证明或近期用户原文可能被改写时，立即停止并报告；不得跳过验证或带着失败继续。
