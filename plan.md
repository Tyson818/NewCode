# Chapter 8：Context Management 实施计划

## 架构概览

Chapter 8 新增一个独立的 Context Management 层，作为 AgentLoop 向主模型发起请求前的会话预处理器。它拥有会话级 context state、近似 token 估算、已知敏感值遮蔽、artifact 生命周期、第一层外置、第二层摘要选择和熔断状态；它不拥有工具执行、权限决策、MCP transport 或 Provider 协议。

主调用路径为：

```text
CLI / AgentLoop
  -> ContextManager.prepare_for_request()
     -> 脱敏与第一层工具结果外置
     -> usage 锚点与上下文估算
     -> 必要时 SummaryGenerator（零工具）与第二层摘要
  -> PromptBuilder
  -> ChatProvider.stream_chat(...)
  -> 收到可信 usage 后 ContextManager.record_usage()
```

`SummaryGenerator` 是由 AgentLoop 注入 Context Management 的窄接口/回调。它仅接收已脱敏的摘要输入和固定摘要指令，返回文本与可选 usage；Context Management 不直接依赖具体 Provider 实现，Provider 也不导入或理解 Context Management。摘要请求显式传入空工具集合，草稿在格式解析后丢弃。

artifact 写入是 Context Management 的内部受控操作，目标必须解析并验证为 workspace sandbox 内的 `.newcode/context-artifacts/<session-id>/`；它不通过 Agent 工具、ToolScheduler、PermissionManager 或模型 tool call。模型只会在边界消息的指引下，使用既有受控读取工具重新读取安全相对路径。

## 核心数据与职责

| 组件 | 职责 |
|---|---|
| `ContextState` | 保存 session id、可信 usage 锚点及会话版本、连续摘要失败数、熔断状态和 artifact 序号。 |
| `TokenEstimator` | 以稳定 JSON、字符估算、消息/tool 开销和 1.20 系数计算近似 token；计算锚点后的增量或全量。 |
| `SensitiveRedactor` | 使用既有敏感值并按敏感字段名递归遮蔽，将结果用于所有 Context Management 输出。 |
| `ArtifactStore` | 在唯一允许的 workspace sandbox 目录中写入、截断、清理和返回安全相对路径。 |
| `PreventionCompressor` | 依据 8K/12K 阈值稳定地选择工具结果、生成 1,200 字符预览并替换会话表示。 |
| `HistoryCompactor` | 选择近期保留区、生成/校验七段式摘要、丢弃草稿、插入边界消息，并维护熔断。 |
| `ContextManager` | 编排每次请求前的固定顺序、usage 锚点失效和手动/自动压缩状态。 |
| `SummaryGenerator` | 从 AgentLoop 注入的零工具摘要调用边界；不执行工具，不读取 artifact。 |

## 文件组织

预计新增：

```text
newcode/context/
├── __init__.py          # 对外导出最小 Context Management 接口
├── types.py             # 状态、估算、artifact、压缩结果等领域类型
├── estimator.py         # 固定近似 token 与 usage 锚点计算
├── redaction.py         # Context Management 专用安全遮蔽
├── artifacts.py         # sandbox 内 artifact 写入、截断、清理
├── prevention.py        # 第一层工具结果外置
├── summary.py           # 摘要 prompt、草稿剥离、七段格式校验
├── history.py           # 近期保留选择、第二层替换、熔断
└── manager.py           # 请求前编排和手动压缩入口
```

预计修改：

- `newcode/session.py`：提供受控的消息替换/版本递增能力，保持既有消息序列和 tool-call/result 配对语义。
- `newcode/agent/loop.py`：在每次主模型请求前调用 ContextManager，并在成功响应后记录 usage；不加入摘要策略或文件写入细节。
- `newcode/cli.py`：创建会话级 ContextManager、处理 `/compact`、在退出路径清理 artifact。
- `newcode/agent/events.py`、`newcode/agent/collector.py`：仅当现有 usage 事件/结果不足以传递可信 prompt/input usage 时作最小扩展。
- `.gitignore`：实现阶段增加运行时 `.newcode/context-artifacts/` 忽略规则，避免 artifact 被提交。

预计新增测试：

```text
tests/test_context_estimator.py
tests/test_context_redaction.py
tests/test_context_artifacts.py
tests/test_context_prevention.py
tests/test_context_history.py
tests/test_context_summary.py
tests/test_context_manager.py
tests/test_agent_loop_context.py
tests/test_cli_context.py
```

## 固定架构边界

- Provider 只保持 Chat Completions/streaming 职责，不导入或持有 Context Management 状态、artifact 路径或摘要策略。
- Context Management 不调用 Agent 工具、不调用 MCP、不绕过 Plan/Do gate、PermissionManager 或 ToolScheduler。
- artifact 写入只允许 workspace sandbox 内的指定会话目录；不得写入用户目录、TEMP、绝对外部路径或任意由模型提供的路径。
- 摘要请求零工具；不得读文件、执行命令、发起 MCP 调用或递归触发压缩。
- 用户近期保留区原文、六个内置工具、MCP、Permission、Prompt 与 Provider 的现有职责和基本行为不得改变。
- 不实现精确 tokenizer、自动模型窗口推断、向量记忆、云同步、网络访问或机器学习式策略优化。

## Phase 1：会话版本、近似 token 与 usage 锚点

### 目标

建立独立、可复现的上下文估算基础，并让会话变更可以使 usage 锚点精确失效。

### 涉及文件

- 新增 `newcode/context/types.py`、`newcode/context/estimator.py`、`tests/test_context_estimator.py`。
- 修改 `newcode/session.py`；如有必要最小修改 `newcode/agent/events.py`、`newcode/agent/collector.py`。

### 依赖

无；是后续所有 Phase 的前置。

### 设计

- 为会话消息序列建立单调版本号；任何外置、摘要替换或其他压缩性重写都会递增版本。
- 按 spec 的稳定 JSON、`ceil(chars / 2)`、消息 12、tool 24 与 1.20 系数实现全量估算。
- 保存可信 prompt/input usage 及其会话版本；版本相同则使用锚点加增量，版本不同则只作全量估算。
- 非法、缺失、负数或非 input/prompt usage 不覆盖现有可信锚点；摘要请求 usage 不写入主会话锚点。

### 测试范围

- 文本、Unicode、结构化消息、tool call/result、边界相等和稳定序列化。
- 锚点增量、非法 usage、会话版本变化与摘要 usage 隔离。
- 既有 ChatSession 基本追加、读取和消息顺序回归。

### 完成标准

- 在无 Provider、无文件系统和无工具调用条件下可完整验证估算。
- 不改变现有公开消息内容或 Provider 行为。

### 不得跨越的边界

不得新增 tokenizer 依赖、模型窗口自动探测或任何压缩/文件写入实现。

## Phase 2：敏感信息遮蔽与安全 artifact 存储

### 目标

实现 Context Management 内部的脱敏与唯一允许的 artifact 写入/清理边界。

### 涉及文件

- 新增 `newcode/context/redaction.py`、`newcode/context/artifacts.py`、`tests/test_context_redaction.py`、`tests/test_context_artifacts.py`。
- 修改 `newcode/context/types.py`、`.gitignore`。

### 依赖

Phase 1 的状态、版本和安全长度估算类型。

### 设计

- 合并既有敏感值集合与递归字段名遮蔽；覆盖 token、secret、password、credential、authorization、cookie、api key 及常见变体。
- artifact 路径以已解析 workspace root 为锚点，拒绝 session id、文件名或符号链接导致的 sandbox 逃逸。
- 只在 `.newcode/context-artifacts/<safe-session-id>/` 写入 JSON-safe、已脱敏记录；文件名使用内部序号和生成标识。
- 20 MiB 上限前截断且记录截断状态；写入失败保留原结果并返回安全失败，而不产生虚假路径。
- 正常退出清理当前会话目录；仅在专用根内清理超过 7 天的陈旧目录。

### 测试范围

- 已知 secret、嵌套敏感键、URL credential、headers、env、异常文本遮蔽。
- 正常写入、20 MiB 截断、相对路径、JSON-safe 失败、写入失败和权限降级行为。
- 绝对路径、`..`、恶意 session id、符号链接和 sandbox 外目标均被拒绝；零 Agent 工具调用。
- 当前会话清理和陈旧目录清理绝不触及专用根之外文件。

### 完成标准

- artifact 内容和返回信息均不含测试 secret。
- 所有写入可证明位于唯一允许路径，且 `.gitignore` 覆盖运行时 artifact 根。

### 不得跨越的边界

不得接入 AgentLoop、CLI、工具执行或摘要；不得向任意临时目录或用户目录写入。

## Phase 3：第一层工具结果预防性外置

### 目标

基于 Phase 1/2，将过大工具结果以稳定、可追溯方式替换为安全预览和 artifact 索引。

### 涉及文件

- 新增 `newcode/context/prevention.py`、`tests/test_context_prevention.py`。
- 修改 `newcode/context/types.py`、`newcode/context/manager.py`（仅预防性入口）及 `newcode/session.py` 的受控替换 API。

### 依赖

Phase 1 和 Phase 2。

### 设计

- 单结果估算 `>= 8,000` 必须外置；单消息工具结果累计 `>= 12,000` 时按大小降序、同值原始顺序继续外置至低于阈值。
- 已外置结果不重复处理；预览最多 1,200 已脱敏字符，包含相对路径和省略/截断状态。
- 仅替换工具结果表示；用户消息和其他非工具内容逐字不变。
- 外置成功才替换会话内容并使 usage 锚点失效；失败保留原内容和安全诊断。

### 测试范围

- 两个阈值的等值/临界值、多个结果排序、稳定同值顺序和重复调用幂等。
- 预览长度、路径、截断/失败状态、敏感信息遮蔽。
- 用户原文、tool-call id、tool-result 配对及原始消息顺序不变。

### 完成标准

- 不接入主模型请求时，纯本地会话即可验证第一层全部行为。
- artifact 写入严格复用 Phase 2，不出现第二套路径或遮蔽策略。

### 不得跨越的边界

不得发起摘要 API 调用、修改 Prompt、调用工具或删除原始 artifact。

## Phase 4：第二层历史选择、结构化摘要与熔断

### 目标

实现可注入 fake SummaryGenerator 的第二层压缩，不接入真实主请求。

### 涉及文件

- 新增 `newcode/context/summary.py`、`newcode/context/history.py`、`tests/test_context_summary.py`、`tests/test_context_history.py`。
- 修改 `newcode/context/types.py`、`newcode/context/manager.py`。

### 依赖

Phase 1 的估算/版本、Phase 2 的遮蔽与索引、Phase 3 的外置表示。

### 设计

- 自动阈值为 51K；从尾部保留“约 10K token”和“至少 5 条消息”中较大的范围，保持完整 tool-call/result 交换、系统约束、边界消息和近期用户消息原文。
- 较早消息可替换为新摘要；仍有效的较早用户约束尽量逐字摘录，且固定标记“原文摘录”或“归纳”。已有摘要参与新摘要输入。
- 固定摘要 prompt 明确零工具、先草稿后正式摘要；解析层丢弃草稿，校验七个必需部分及证据/推断/未确认标识。
- 成功后替换可压缩历史、插入安全边界消息、失效 usage 锚点；失败不修改会话。
- 连续失败 3 次打开熔断；自动请求停止摘要，单次手动重试可尝试一次，成功重置熔断。

### 测试范围

- 51K 触发、无可压缩历史、尾部选择、完整交换、近期用户原文、旧摘要再摘要。
- 七段格式、草稿丢弃、零工具参数、敏感信息不进入摘要/边界消息。
- 成功替换、失败原子性、三次熔断、成功重置、熔断后单次手动重试。

### 完成标准

- fake generator 可证明没有工具执行，且每次压缩最多调用一次。
- 所有摘要与边界消息均能通过固定格式与脱敏断言。

### 不得跨越的边界

不得调用真实网络、Provider 具体实现、MCP、文件读取工具或 CLI。

## Phase 5：AgentLoop 请求前接入与 usage 回写

### 目标

将 ContextManager 接入每次主模型请求前的唯一位置，并使其跨同一会话的多轮和工具循环保持状态。

### 涉及文件

- 修改 `newcode/agent/loop.py`、`newcode/agent/collector.py`、`newcode/agent/events.py`（仅必要时）。
- 新增 `tests/test_context_manager.py`、`tests/test_agent_loop_context.py`。
- 修改 `newcode/context/manager.py`、`newcode/context/__init__.py`。

### 依赖

Phase 1–4。

### 设计

- AgentLoop 在每次 `stream_chat` 前按“新增消息/工具结果 → 第一层 → 估算 → 至多一次第二层 → 构造请求”的顺序调用 ContextManager。
- 主模型成功返回可信 usage 后回写锚点；Provider error、取消或不可信 usage 不更新锚点。
- SummaryGenerator 由 AgentLoop 注入，摘要调用使用空 tools，不复用主轮的工具许可，也不递归调用 prepare。
- ContextManager 通过依赖注入跨 AgentLoop 实例共享，由 CLI 的同一 ChatSession 持有；直接使用 AgentLoop 的调用者仍可提供或不提供该可选组件。

### 测试范围

- 多轮、含工具循环的请求前顺序和 usage 回写。
- 自动摘要只触发一次、摘要调用零工具、主请求保留既有 tools。
- 大工具结果外置后主模型看到预览/路径；边界消息促使 fake 模型走既有读取工具路径。
- Plan/Do、Permission、ToolScheduler、MCP adapter 和内置工具链路无 bypass、无回归。

### 完成标准

- 全部压缩发生在 Provider 主请求之前，且 Provider 无 Context Management 导入。
- AgentLoop 现有取消、错误、工具排序和 session 语义测试全部保持通过。

### 不得跨越的边界

不得让 Provider 管理状态或 artifacts，不得让 ContextManager 执行工具、权限确认或 MCP 调用。

## Phase 6：CLI `/compact`、会话生命周期与 artifact 清理

### 目标

向用户暴露安全的手动压缩入口，并在所有 CLI 退出路径清理会话 artifact。

### 涉及文件

- 修改 `newcode/cli.py`。
- 新增 `tests/test_cli_context.py`。
- 视输出格式需要最小修改 `newcode/context/manager.py`。

### 依赖

Phase 1–5。

### 设计

- `run_conversation` 为一个 ChatSession 创建并复用一个 ContextManager；创建时绑定该 session 的 workspace root 和敏感值。
- 新增 `/compact` 命令：先运行第一层；存在可压缩历史时必须强制一次摘要，61K 只用于状态/建议，不阻止手动执行。
- 没有可压缩历史、摘要失败或熔断时输出安全状态，不输出 prompt、artifact 内容、secret、headers、env 或完整异常。
- `main`/交互 lifecycle 的 finally 覆盖 EOF、`/exit`、KeyboardInterrupt 与异常；清理仅限当前会话 artifact 目录。现有 MCP shutdown 语义保持。

### 测试范围

- `/compact` 的强制优先级、61K 状态建议、无历史、成功、失败与熔断重试。
- EOF、`/exit`、KeyboardInterrupt、AgentLoop 异常后的 artifact 清理。
- 无 `/compact` 使用时既有 CLI 行为和 `/plan`、`/do`、`/exit` 行为不变。
- CLI 输出不泄露敏感值、路径外文件、摘要草稿或远端错误。

### 完成标准

- CLI 不直接实现选择/摘要/写入细节，只调用 ContextManager。
- 正常与异常退出均不会遗留当前会话 artifact。

### 不得跨越的边界

不得改变 MCP lifecycle、Permission confirmer、Provider 配置或现有命令含义；不得使用 tmux 安装或真实生产 secret。

## Phase 7：全量回归与人工验收

### 目标

以自动化和受控本地 CLI 场景证明 Context Management 满足规格且没有破坏 Chapter 5–7 能力。

### 涉及文件

- 原则上只新增最小回归测试或修复被测试证明的缺陷。
- 涉及所有 Chapter 8 测试与既有 Agent、CLI、Prompt、Permission、MCP、Provider 测试。

### 依赖

Phase 1–6 的 targeted tests 全部通过。

### 测试范围

- `python -m compileall newcode`。
- Chapter 8 全部 targeted pytest。
- 全量 `pytest -q -rs --basetemp <系统 TEMP 专用目录>`，记录任何 skip 的环境原因。
- `git diff --check`。
- 人工本地验收仅用 fake provider/local fixture：普通会话、超大工具结果、自动摘要、`/compact`、三次失败熔断、退出清理和敏感输出检查。

### 完成标准

- 全量编译、测试、差异检查通过；任何 skipped 项有明确、可复现的环境原因。
- 人工验收不使用真实网络、生产 secret 或第三方 MCP server。
- 所有 artifact 留在 sandbox，退出后当前会话目录已清理。

### 不得跨越的边界

不得以人工验收替代自动化回归；不得引入精确 tokenizer、自动重连、长期记忆、网络 sandbox 或本章外功能。

## 阶段依赖图

```text
Phase 1（估算与锚点）
  -> Phase 2（遮蔽与 artifact）
  -> Phase 3（第一层外置）
  -> Phase 4（摘要与熔断）
  -> Phase 5（AgentLoop 接入）
  -> Phase 6（CLI /compact 与清理）
  -> Phase 7（全量回归与人工验收）
```

每个 Phase 必须先运行其列出的 targeted tests；失败、违反安全边界、需要扩大文件范围或发现摘要 API 无法维持零工具边界时，停止后报告，不得带着失败进入下一 Phase。
