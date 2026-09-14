# Chapter 8：Context Management 执行 Checklist

## 全局门禁

- [ ] 只在上一 Phase 的 compileall 与 targeted pytest 均通过后进入下一 Phase。
- [ ] 所有 pytest 使用系统 TEMP：`--basetemp "$env:TEMP\newcode-pytest-context-<phase>"`。
- [ ] 摘要请求零工具、零文件读取、零 MCP，且不递归触发压缩；自动或手动的一次请求最多一次摘要尝试。
- [ ] artifact 只允许内部受控写入 workspace sandbox 内 `.newcode/context-artifacts/<session-id>/`；不经 Agent 工具、ToolScheduler、PermissionManager 或模型 tool call，且不得写其他路径。
- [ ] 近期保留区用户消息逐字保留；较早用户约束仅可标为“原文摘录”或“归纳”。
- [ ] Provider 不理解 Context Management；Context Management 不绕过 MCP、Permission、Plan/Do 或 ToolScheduler。

## Phase 1：估算与 usage 锚点

### T1：类型与会话版本

- [ ] 前置依赖：已批准 spec/plan/task。
- [ ] 允许文件：`newcode/context/__init__.py`、`newcode/context/types.py`、`newcode/session.py`、`tests/test_context_estimator.py`；必要时 `tests/test_session.py`。
- [ ] 核心核对：session id、会话版本、锚点和压缩状态类型完备；压缩替换递增版本；消息顺序、tool-call/result 配对、用户原文不变。
- [ ] 禁止边界：不实现估算、artifact、摘要、AgentLoop 接入。
- [ ] compileall：`.\.venv\Scripts\python.exe -m compileall newcode`
- [ ] targeted pytest：`.\.venv\Scripts\python.exe -m pytest tests/test_context_estimator.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase1"`
- [ ] 完成证据：版本/追加/替换配对测试通过，无 Provider、文件或工具调用。

### T2：近似估算与锚点

- [ ] 前置依赖：T1。
- [ ] 允许文件：`newcode/context/estimator.py`、`newcode/context/types.py`、`newcode/context/__init__.py`、`tests/test_context_estimator.py`。
- [ ] 核心核对：稳定 JSON、字符/消息/tool 开销和 1.20 系数；可信 usage 增量锚定；压缩版本变化失效；非法/摘要 usage 不覆盖。
- [ ] 禁止边界：不新增 tokenizer、窗口自动探测或 Provider 修改。
- [ ] compileall：同 Phase 1。
- [ ] targeted pytest：同 Phase 1。
- [ ] 完成证据：Unicode、阈值等值、增量/失效/非法 usage 测试通过。

## Phase 2：脱敏与 artifact

### T3：敏感信息遮蔽

- [ ] 前置依赖：Phase 1。
- [ ] 允许文件：`newcode/context/redaction.py`、`newcode/context/types.py`、`tests/test_context_redaction.py`。
- [ ] 核心核对：已知值与敏感字段递归遮蔽；预览、artifact、摘要、边界消息和诊断统一处理。
- [ ] 禁止边界：不改写用户原文，不写文件，不接工具/CLI。
- [ ] compileall：`.\.venv\Scripts\python.exe -m compileall newcode`
- [ ] targeted pytest：`.\.venv\Scripts\python.exe -m pytest tests/test_context_redaction.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase2"`
- [ ] 完成证据：secret、headers、env、URL credential 和异常文本均不泄露。

### T4：sandbox artifact 写入与清理

- [ ] 前置依赖：T3。
- [ ] 允许文件：`newcode/context/artifacts.py`、`newcode/context/types.py`、`.gitignore`、`tests/test_context_artifacts.py`。
- [ ] 核心核对：仅写 sandbox 内指定会话目录；安全文件名、JSON-safe/脱敏记录、20 MiB 截断；仅清理当前会话和专用根内陈旧目录。
- [ ] 禁止边界：拒绝绝对路径、`..`、符号链接逃逸、TEMP/用户目录/模型路径；零 Agent 工具调用。
- [ ] compileall：同 Phase 2。
- [ ] targeted pytest：`.\.venv\Scripts\python.exe -m pytest tests/test_context_redaction.py tests/test_context_artifacts.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase2"`
- [ ] 完成证据：路径逃逸和写入失败不生成虚假路径；清理绝不触及 sandbox 外文件；artifact 根被忽略。

## Phase 3：第一层外置

### T5：8K 单结果外置

- [ ] 前置依赖：Phase 2。
- [ ] 允许文件：`newcode/context/prevention.py`、`newcode/context/types.py`、`newcode/session.py`、`tests/test_context_prevention.py`。
- [ ] 核心核对：`>=8,000` 外置；仅替换工具结果为最多 1,200 字已脱敏预览、路径和状态；成功才失效锚点。
- [ ] 禁止边界：不调用 SummaryGenerator、Provider 或 Agent 工具；不改写用户/非工具消息。
- [ ] compileall：`.\.venv\Scripts\python.exe -m compileall newcode`
- [ ] targeted pytest：`.\.venv\Scripts\python.exe -m pytest tests/test_context_prevention.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase3"`
- [ ] 完成证据：8K 边界、预览长度、脱敏、失败原子性和配对保持通过。

### T6：12K 累计稳定选择

- [ ] 前置依赖：T5。
- [ ] 允许文件：`newcode/context/prevention.py`、`newcode/context/types.py`、`tests/test_context_prevention.py`。
- [ ] 核心核对：`>=12,000` 时按结果大小降序、同值按原 tool-call 顺序外置至低于阈值；重复调用幂等。
- [ ] 禁止边界：不发起第二层摘要，不改 Prompt/CLI，不改写用户原文。
- [ ] compileall：同 Phase 3。
- [ ] targeted pytest：同 Phase 3。
- [ ] 完成证据：排序、同值、12K 等值和未外置结果保留测试通过。

## Phase 4：摘要、边界消息与熔断

### T7：近期保留与摘要输入

- [ ] 前置依赖：Phase 3。
- [ ] 允许文件：`newcode/context/history.py`、`newcode/context/summary.py`、`newcode/context/types.py`、`tests/test_context_history.py`、`tests/test_context_summary.py`。
- [ ] 核心核对：51K 自动阈值；保留 10K/5 条较大范围、完整工具交换、系统/边界消息及近期用户逐字原文；较早约束标为原文摘录或归纳。
- [ ] 禁止边界：不调用真实 Provider、网络、读取工具或 CLI。
- [ ] compileall：`.\.venv\Scripts\python.exe -m compileall newcode`
- [ ] targeted pytest：`.\.venv\Scripts\python.exe -m pytest tests/test_context_history.py tests/test_context_summary.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase4"`
- [ ] 完成证据：51K、无历史、完整配对、近期原文和再摘要选择测试通过。

### T8：双包络零工具摘要与边界消息

- [ ] 前置依赖：T7。
- [ ] 允许文件：`newcode/context/summary.py`、`newcode/context/history.py`、`tests/test_context_summary.py`、`tests/test_context_history.py`。
- [ ] 核心核对：请求仅允许 `<analysis_draft>...</analysis_draft><structured_summary>...</structured_summary>`；只保存/校验 structured_summary；七段、证据/推断/未确认完整；成功插入重新读取 artifact 的边界消息。
- [ ] 禁止边界：摘要请求零工具、零 artifact 读取、零递归压缩；draft 和完整原始响应不得写入 session/artifact/日志/CLI。
- [ ] compileall：同 Phase 4。
- [ ] targeted pytest：`.\.venv\Scripts\python.exe -m pytest tests/test_context_history.py tests/test_context_summary.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase4"`
- [ ] 完成证据：缺失/重复/顺序错/包络外非空/结构缺失均失败且会话不变；一次请求最多一次摘要尝试。

### T9：三次失败熔断与手动单次重试

- [ ] 前置依赖：T8。
- [ ] 允许文件：`newcode/context/manager.py`、`newcode/context/types.py`、`newcode/context/history.py`、`tests/test_context_manager.py`。
- [ ] 核心核对：连续三次失败开路并停止自动摘要；成功归零；熔断后 `/compact` 只显式单次重试。
- [ ] 禁止边界：不实现 CLI 命令、不调用真实 API/工具、不循环重试。
- [ ] compileall：同 Phase 4。
- [ ] targeted pytest：`.\.venv\Scripts\python.exe -m pytest tests/test_context_history.py tests/test_context_summary.py tests/test_context_manager.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase4"`
- [ ] 完成证据：三次失败、自动禁用、成功重置、手动单次成功/失败均通过。

## Phase 5：AgentLoop 请求前接入

### T10：请求前顺序与 usage 回写

- [ ] 前置依赖：Phase 4。
- [ ] 允许文件：`newcode/agent/loop.py`，必要时 `newcode/agent/collector.py`、`newcode/agent/events.py`，`newcode/context/manager.py`、`newcode/context/__init__.py`，`tests/test_agent_loop_context.py`、`tests/test_context_manager.py`，必要时 `tests/test_agent_loop.py`。
- [ ] 核心核对：每个主请求前依序第一层→估算→至多一次第二层→主请求；可信主响应 usage 回写；注入式摘要请求零工具/不递归。
- [ ] 禁止边界：Provider 不导入 Context；Context 不执行工具、权限确认或 MCP；不改变 Plan/Do、Permission、ToolScheduler。
- [ ] compileall：`.\.venv\Scripts\python.exe -m compileall newcode`
- [ ] targeted pytest：`.\.venv\Scripts\python.exe -m pytest tests/test_context_manager.py tests/test_agent_loop_context.py tests/test_agent_loop.py tests/test_agent_loop_mcp.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase5"`
- [ ] 完成证据：多轮、工具循环、usage、取消/Provider error 与主请求 tools 回归通过。

### T11：端到端工具流不绕过

- [ ] 前置依赖：T10。
- [ ] 允许文件：`tests/test_agent_loop_context.py`，必要时 `tests/test_agent_loop.py`、`tests/test_agent_loop_mcp.py`。
- [ ] 核心核对：大结果外置后模型仅见预览/路径；边界消息后经既有 read_file 读取；Plan/Do、Permission、MCP、Scheduler gates 持续生效。
- [ ] 禁止边界：不用真实网络、生产 secret 或第三方 MCP。
- [ ] compileall：同 Phase 5。
- [ ] targeted pytest：同 Phase 5。
- [ ] 完成证据：外置→读取→回答流、拒绝零远端调用、串行和六工具回归通过。

## Phase 6：CLI `/compact` 与清理

### T12：会话级 ContextManager 与 `/compact`

- [ ] 前置依赖：Phase 5。
- [ ] 允许文件：`newcode/cli.py`、`newcode/context/manager.py`、`tests/test_cli_context.py`，必要时 `tests/test_cli_tool_flow.py`。
- [ ] 核心核对：会话复用 manager；`/compact` 先第一层，存在历史即强制一次摘要；61K 仅状态/建议。
- [ ] 禁止边界：不改 MCP lifecycle、Permission confirmer、Provider 配置或既有命令含义；不泄露 prompt/artifact/secret/headers/env。
- [ ] compileall：`.\.venv\Scripts\python.exe -m compileall newcode`
- [ ] targeted pytest：`.\.venv\Scripts\python.exe -m pytest tests/test_cli_context.py tests/test_cli_tool_flow.py tests/test_cli_mcp.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase6"`
- [ ] 完成证据：强制优先级、61K 提示、无历史、失败/熔断重试及 Plan/Do/exit 回归通过。

### T13：CLI 所有退出路径清理

- [ ] 前置依赖：T12。
- [ ] 允许文件：`newcode/cli.py`、`tests/test_cli_context.py`。
- [ ] 核心核对：finally 清理当前会话 artifact；EOF、`/exit`、中断、AgentLoop/启动后异常覆盖；失败隔离。
- [ ] 禁止边界：不得清理 sandbox 外、其他 session 或工作区文件；不安装 tmux，不用真实网络/生产 secret。
- [ ] compileall：同 Phase 6。
- [ ] targeted pytest：同 Phase 6。
- [ ] 完成证据：各退出路径清理、其他文件保留、脱敏输出和 MCP shutdown 回归通过。

## Phase 7：全量回归与受控人工验收

### T14：自动回归与边界审计

- [ ] 前置依赖：Phase 1–6 targeted pytest 全部通过。
- [ ] 允许文件：原则上无；仅允许修复失败直接证明的最小模块及其回归测试。
- [ ] 核心核对：Provider 不导入 Context；Context 不导入工具/MCP/权限执行层；artifact sandbox、近期用户原文、双包络、零工具/不递归及熔断边界均可审计。
- [ ] 禁止边界：不以重构无关模块或放宽安全边界换取通过。
- [ ] compileall：`.\.venv\Scripts\python.exe -m compileall newcode`
- [ ] targeted pytest：`.\.venv\Scripts\python.exe -m pytest tests/test_context_estimator.py tests/test_context_redaction.py tests/test_context_artifacts.py tests/test_context_prevention.py tests/test_context_history.py tests/test_context_summary.py tests/test_context_manager.py tests/test_agent_loop_context.py tests/test_cli_context.py -q --basetemp "$env:TEMP\newcode-pytest-context-phase7"`
- [ ] 完成证据：全部 Chapter 8 targeted tests 和相关 Agent/CLI/Prompt/Permission/MCP/Provider 回归通过。

### T15：全量验证、skip 记录与人工验收

- [ ] 前置依赖：T14。
- [ ] 允许文件：无；发现问题先补最小自动化回归测试，再修复最小缺陷。
- [ ] 核心核对：fake provider/local fixture 验收普通会话、外置、自动摘要、`/compact`、三次熔断、artifact 清理、敏感输出与重新读取路径。
- [ ] 禁止边界：人工验收不用真实网络、生产 secret、第三方 MCP；不安装 tmux。环境无 tmux 时记录原因并以自动化 CLI fixture 为依据。
- [ ] compileall：`.\.venv\Scripts\python.exe -m compileall newcode`
- [ ] targeted pytest：`.\.venv\Scripts\python.exe -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-context-final"`
- [ ] 完成证据：全量 pytest 结果、每项 skipped 的可复现环境原因、`git diff --check` 通过、人工验收记录和退出后 artifact 不存在。

## Phase 退出门

- [ ] Phase 1：T1–T2 证据齐全。
- [ ] Phase 2：T3–T4 证据齐全，sandbox/清理范围已验证。
- [ ] Phase 3：T5–T6 证据齐全，用户原文未改写。
- [ ] Phase 4：T7–T9 证据齐全，双包络、零工具、一次尝试和熔断已验证。
- [ ] Phase 5：T10–T11 证据齐全，Provider、MCP、Permission、ToolScheduler 未被绕过。
- [ ] Phase 6：T12–T13 证据齐全，`/compact` 强制优先级和退出清理已验证。
- [ ] Phase 7：T14–T15 证据齐全，全量回归、skip 原因和受控人工验收已记录。
