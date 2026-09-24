# Chapter 13：SubAgent 原子任务

## 通用执行规则

- 先完成前置任务；每个 Phase 通过 `.venv\Scripts\python.exe -m compileall newcode` 与本 Phase targeted pytest 后才进入下一 Phase。
- 测试仅使用 fake Provider/ProviderFactory、fake clock/thread coordination、临时 home/workspace 和本地 fixture；不访问真实网络、不读取生产 secret、不连接第三方 MCP。
- 如 `.venv` 不存在，可使用当前 Python，但须记录。禁止跨 Phase 文件、Provider orchestration、Permission 语义或历史章节范围扩展。
- 每项完成时记录测试输出、稳定错误码、状态/安全边界证据；测试失败立即停止，不带失败进入下一 Phase。

## 文件规划

| 操作 | 文件 | 职责 |
|---|---|---|
| 新增 | `newcode/subagents/__init__.py` | 导出稳定窄接口 |
| 新增 | `newcode/subagents/types.py` | 定义、Task、状态、预算、错误和诊断模型 |
| 新增 | `newcode/subagents/discovery.py` | YAML/Markdown 定义发现、优先级、路径验证 |
| 新增 | `newcode/subagents/policy.py` | 父/角色/mode/background 工具集合交集 |
| 新增 | `newcode/subagents/budget.py` | token 近似统计、round/deadline budget |
| 新增 | `newcode/subagents/manager.py` | 有界 worker、task 状态、通知、wait/cancel/close |
| 新增 | `newcode/subagents/runner.py` | 隔离 child AgentLoop、Definition/Fork 快照与 cleanup |
| 新增 | `newcode/subagents/tool.py` | 唯一固定 `agent` Tool schema 与操作分派 |
| 修改 | `newcode/agent/loop.py` | 主请求安全点投递后台完成消息及 AgentTool 接缝 |
| 修改 | `newcode/prompt/modules.py` | 仅 Agent name/description 动态目录 |
| 修改 | `newcode/cli.py` | ProviderFactory、Manager/Tool 注册与生命周期 |
| 必要时修改 | `newcode/skills/policy.py` | 提供只读的父 Skill 有效工具集合 |
| 新增测试 | `tests/test_subagents_types.py`, `test_subagents_discovery.py`, `test_subagents_budget.py`, `test_subagents_manager.py`, `test_subagents_policy.py`, `test_subagents_runner.py`, `test_subagents_tool.py`, `test_prompt_subagents.py`, `test_agent_loop_subagents.py`, `test_cli_subagents.py` | 覆盖各层 |
| 必要时修改测试 | 现有 `tests/test_hooks_actions.py`、AgentLoop/CLI 回归 | 证明 Hook 占位语义和旧行为不变 |

禁止新增依赖、修改 `newcode/providers/`、`newcode/permissions/`、`newcode/mcp/`、`newcode/persistence.py`、session archive 格式、配置文件或 Chapter 10 命令注册表。若确需上述范围，停止并报告最小阻塞点。

## Phase 1：定义模型与发现（T1–T4）

### T1 — AgentDefinition 与诊断类型

**前置条件：** Chapter 13 spec 已批准。
**允许文件：** `newcode/subagents/__init__.py`、`newcode/subagents/types.py`、`tests/test_subagents_types.py`。
**实现动作：** 定义 source、frontmatter、catalog、diagnostic、模型/权限模式与稳定名称格式 `[a-z][a-z0-9-]{0,63}`；限制字段类型、重复项、描述长度、正文大小和 `max_iterations=1..8`；定义稳定错误码。
**对应测试：** 字段缺失/未知/类型错误、名称边界、duplicate tool、iteration 边界、错误码稳定；确保 repr/diagnostic 不含正文。
**完成判定：** 纯类型可独立导入，非法输入只产生稳定 validation code。
**禁止边界：** 不扫描文件、不构造 Tool、不启动 Provider/worker。

### T2 — 安全目录扫描

**前置条件：** T1。
**允许文件：** `newcode/subagents/discovery.py`、`tests/test_subagents_discovery.py`。
**实现动作：** 扫描 project `.newcode/agents/`、user `~/.newcode/agents/`、package `newcode/resources/agents/` 与显式注入的 plugin roots；只读普通 `.md` 文件，不导入/执行插件代码。
**对应测试：** 临时根读取、缺根、绝对路径/`..`、符号链接、目录逃逸、非普通文件与权限/OSError。
**完成判定：** 所有 entry 在各自来源根内；无效文件有脱敏诊断且不阻断其他文件。
**禁止边界：** 不扫描任意系统目录，不修改 workspace，不下载插件。

### T3 — YAML frontmatter 与正文校验

**前置条件：** T1–T2。
**允许文件：** `newcode/subagents/discovery.py`、`types.py`、`tests/test_subagents_types.py`、`tests/test_subagents_discovery.py`。
**实现动作：** 严格解析 `name/description/tools.allow/tools.deny/model/max_iterations/permission_mode`；`tools.allow` 必须是字符串列表，空列表明确表示不允许普通工具；`tools.deny` 可选且缺省为空，deny 优先；未知字段拒绝；检查 Markdown 分隔符、正文 ≤64 KiB、工具名唯一与格式；同来源同名冲突隔离。
**对应测试：** 有效/无效 frontmatter、allow 缺失/空列表/错误类型、deny 缺省/显式覆盖与 deny 优先、超限正文、重复名、插件路径注入、未知字段；重复名不由扫描顺序随机选中。
**完成判定：** 每条定义独立解析；缺少 allow 与显式空 allow 有不同且固定语义；bad definition 不影响 sibling。
**禁止边界：** 不执行 Markdown/资源中的 script，不对 Agent 正文做模板求值。

### T4 — 来源覆盖与启动可用性

**前置条件：** T1–T3；测试可注入最终 ToolRegistry 名称。
**允许文件：** `newcode/subagents/discovery.py`、`tests/test_subagents_discovery.py`。
**实现动作：** 实现 project > user > builtin > explicit plugin precedence；跨来源以高优先级有效定义覆盖，单文件非法不屏蔽低层有效项；同源 duplicate 全部冲突项跳过；校验 allow/deny 引用当前 Registry。
**对应测试：** 每层同名覆盖、稳定排序、非法高层回退、未知工具拒绝、plugin 默认空且仅显式注入可见。
**完成判定：** 多次发现输出一致，目录向模型只投影 name/description。
**禁止边界：** 不注入正文、工具 schema、路径、digest 或 secret 到启动 prompt。

**Phase 1 退出门：** `.venv\Scripts\python.exe -m compileall newcode` 与 `pytest tests/test_subagents_types.py tests/test_subagents_discovery.py -q -rs --basetemp "$env:TEMP\newcode-pytest-ch13-p1"` 全通过；路径/来源安全测试有证据。

## Phase 2：预算、任务 manager 与状态机（T5–T8）

### T5 — 状态、scope、预算纯模型

**前置条件：** Phase 1 通过。
**允许文件：** `types.py`、`budget.py`、`tests/test_subagents_budget.py`、`tests/test_subagents_types.py`。
**实现动作：** 定义 queued/running/background/completed/failed/cancelled/timed_out 状态、合法转换、不可复用 task ID 与 session-generation binding；实现 max 8 rounds、300s、16K 近似 token 预算和输入/输出统计；明确 128 条记录上限与 `subagent_task_store_full`。
**对应测试：** 状态图允许/禁止转换、token 估算、边界、usage 不可用、deadline/cancel 预算。
**完成判定：** 超限永不继续下一轮/下一工具；美元成本字段不伪造。
**禁止边界：** 不请求 Provider、不写 archive、不改变 session。

### T6 — 有界 worker 与队列

**前置条件：** T5。
**允许文件：** `manager.py`、`types.py`、`tests/test_subagents_manager.py`。
**实现动作：** 固定 2 daemon workers、最多 4 queued、每 session ≤6 active/uncollected tasks、manager 最多 128 条记录；start 快速入队并返回 task ID；满载立即安全拒绝；只淘汰最旧已领取终态记录，捕获 worker exception。
**对应测试：** 并发上限、队列满、session/record limit、无可淘汰记录时 `subagent_task_store_full`、已领取终态淘汰、未领取结果保留、task id 唯一性、异常隔离、稳定创建/完成序号。
**完成判定：** 不会无界创建线程、任务或诊断，未领取结果绝不被淘汰。
**禁止边界：** 不接 AgentLoop/CLI，不让 worker 写 parent ChatSession。

### T7 — wait/background/cancel/collect 生命周期

**前置条件：** T5–T6。
**允许文件：** `manager.py`、`types.py`、`tests/test_subagents_manager.py`。
**实现动作：** `wait` ≤30s、wait timeout 自动转 background；支持显式后台 start 和 parent Agent `background` 手动转换；终结态 cancel 幂等；completed result 只能领取一次。
**对应测试：** 明确状态转换、假时钟 timeout、三种 background 进入方式、cancel 与 completion race、重复 collect。
**完成判定：** 锁保护状态与结果原子性，不存在终结后被 worker 覆盖。
**禁止边界：** 不实现用户 slash command，不向 session 写消息。

### T8 — session scope、通知与 shutdown

**前置条件：** T6–T7。
**允许文件：** `manager.py`、`types.py`、`tests/test_subagents_manager.py`。
**实现动作：** 完成结果按序写进 manager 有界记录/通知队列；scope mismatch 丢弃；通知/collect 互斥；session close 撤销 token、清空 pending；shutdown 合计 deadline ≤1 秒。
**对应测试：** session ID+generation 不串线、completion 顺序、不丢/不重、关闭超时、close/new scope/reset、不持久化重启。
**完成判定：** manager 独自持有 task state；旧 scope 无可领取结果。
**禁止边界：** 不触碰父 ChatSession/SessionArchive/MCP/Hook cleanup。

**Phase 2 退出门：** compileall 通过；`pytest tests/test_subagents_budget.py tests/test_subagents_manager.py -q -rs --basetemp "$env:TEMP\newcode-pytest-ch13-p2"` 全通过；队列/关闭时间有实际断言。

## Phase 3：策略与 child runner（T9–T13）

### T9 — 多层工具策略交集

**前置条件：** Phase 2 通过；发现 catalog 已完成。
**允许文件：** `policy.py`、必要时 `skills/policy.py`、`tests/test_subagents_policy.py`。
**实现动作：** Definition 使用必填 `tools.allow`（空列表为空普通工具集）；Fork 请求未提供 allowlist 时继承启动时父级有效可见工具不可变快照，提供时仅进一步收窄。计算 Registry ∩ child 启动父级可见/Permission 上限 ∩ 最新 Manager 父策略快照 ∩ Definition/Fork allow − 所有 deny ∩ child mode ∩ background read-only；`agent/load_skill` 不进入 child。父主线程负责发布不可变快照，worker 只能从 Manager 读取；每轮模型请求前和每次工具调用前复核，更新不得扩权；session scope 关闭撤销快照并取消 child。权限模式取更保守者。
**对应测试：** Definition allow 缺失拒绝、空 allow、Fork allow 缺省继承与显式收窄/空集；Plan/Do、allow/deny、deny 优先、空交集、父扩权不扩 child、运行中父 Do→Plan 收窄、运行中 Skill whitelist 收窄、MCP 与 background 过滤；父对象变更不被 worker 直接读取。
**完成判定：** 新工具默认不可用；父级收窄后运行 child 的下一轮模型工具集合及下一次工具调用不再包含被移除工具；parent 后续变宽不能扩大 spawn ceiling；session 关闭撤销策略并取消 child。
**禁止边界：** 不改 Permission 规则语义，不以 whitelist 代替 Permission。

### T10 — 子 Agent 独立 Permission 与 ToolRegistry view

**前置条件：** T9。
**允许文件：** `runner.py`、`policy.py`、`tests/test_subagents_runner.py`、`tests/test_subagents_policy.py`。
**实现动作：** 为 child 构造过滤 registry view 与独立 PermissionManager；复制项目/用户规则与不可变父 Permission 上限，不复制 parent session allow；每轮和每次工具调用读取 Manager 当前策略快照；使用 deny-by-default 非交互 confirmer；不把 AgentTool/递归能力加入 child。
**对应测试：** explicit deny/allow、confirmation 零执行、hard deny/sandbox、无父 session allow 继承、非交互运行；运行中的 child 在父 Do→Plan、Skill allowlist 收窄后下一轮不再看到/调用被移除工具。
**完成判定：** child 每次工具仍由 AgentLoop → Permission → ToolScheduler → executor 判断。
**禁止边界：** 不绕过或改写 PermissionManager，不自动批准 confirmation。

### T11 — Definition runner 与 Context 生命周期

**前置条件：** T9–T10。
**允许文件：** `runner.py`、必要时 `types.py`、`tests/test_subagents_runner.py`。
**实现动作：** 用 definition 正文作为角色 system SOP，为每 task 构造独立 session/loop/context/skills state/usage/cancel state；无 Memory/Hook/session archive；finally 清理 child Context artifact。
**对应测试：** 独立 messages/state、正文每轮存在、Context 独立压缩/失败、cleanup 在成功/异常/cancel 路径运行。
**完成判定：** worker 只向 Manager 返回结果，不接触 parent Session。
**禁止边界：** 不共享 active Skills、HookEngine 或 MemoryService。

### T12 — Fork 快照、模型选择与 cache capability

**前置条件：** T11；ProviderFactory 接口定义。
**允许文件：** `runner.py`、`types.py`、`tests/test_subagents_runner.py`。
**实现动作：** 复制最近 ≤20 条脱敏纯 user/assistant 文本、总长 ≤12,000 字符；冻结父启动 Permission 与有效可见工具上限；禁止 tool/system/dynamic/Hook/Memory 内容；Fork 未给 allowlist 时继承该启动工具快照，显式 allowlist 仅收窄；default inherit model，Fork 可选 model override；model 必须 Factory 声明并显式建 client；无 fallback；cache 能力缺省 false。
**对应测试：** 0/20 条边界、敏感值、无 tool/system/SOP 复制、父 session 修改不影响 child、unsupported `haiku/sonnet/opus` 无 fallback、cache unsupported 普通运行。
**完成判定：** fork snapshot immutable，当前 DeepSeek 仅当前 config model 可用。
**禁止边界：** 不修改 Provider contract/实现，不宣称 token/cost savings。

### T13 — 文件读 cache、MCP/Skill 限定与预算执行

**前置条件：** T9–T12。
**允许文件：** `runner.py`、`budget.py`、必要时 `policy.py`、`tests/test_subagents_runner.py`、`tests/test_subagents_policy.py`。
**实现动作：** 建 child-local read_file cache（128 entry/2 MiB，stat 签名，写/replace/command/MCP 后清空）；每次命中仍先过 Permission/sandbox；检查每次主请求和每个工具前 budget/cancel；MCP 仍走现 manager；Skill state child-local且不继承。
**对应测试：** cache hit/miss/invalidation/symlink/scope isolation，permission denial 不读缓存，MCP confirmation fail-closed，background 仅只读。
**完成判定：** token/round/time超限稳定终止，工具历史完整、安全排序；child memory/service 无共享。
**禁止边界：** 不增大权限、不改 MCP/Context/Memory core、不做 Worktree。

**Phase 3 退出门：** compileall 通过；`pytest tests/test_subagents_policy.py tests/test_subagents_runner.py -q -rs --basetemp "$env:TEMP\newcode-pytest-ch13-p3"` 全通过；fork 隐私、Permission、cache、安全 cleanup 有明确断言。

## Phase 4：统一 Tool 与 AgentLoop/Prompt 接入（T14–T18）

### T14 — 固定 `agent` Tool schema

**前置条件：** Phase 2–3 通过。
**允许文件：** `tool.py`、`types.py`、`tests/test_subagents_tool.py`。
**实现动作：** 实现单一 `agent` Tool 和固定字段 schema。所有请求必填 operation；start 必填 kind、非空 task_prompt、execution；Definition 必填 agent_name 且禁止 model_override/allowlist，Fork 禁止 agent_name、允许可选 model_override/allowlist；start 仅接受这些 operation 专属字段。其他操作必填 task_id：status 只读；wait 可选 1–30 整数 wait_seconds（缺省 30）；background、cancel、collect 不接受其他字段。未知/缺失/不兼容字段统一 `subagent_invalid_request`。定义各操作合法状态、等待/后台转换、取消幂等、结果单次领取及稳定 not-found/not-ready/already-collected/model-unavailable 错误码。
**对应测试：** 固定 schema snapshot；六种 operation 的必填/可选字段、Definition/Fork 差异、每个非法组合与未知字段、wait 边界、状态与领取语义、错误码稳定、无动态注册。
**完成判定：** 任意角色/任务仅有同一 `agent` 名和 schema；所有字段组合确定、可解析并安全失败，不存在动态 schema。
**禁止边界：** Tool 不直接调用 Provider/CLI/UI，不自行绕过 Permission。

### T15 — 主 AgentLoop 注册、Plan/Do 与 Permission gate

**前置条件：** T14。
**允许文件：** `agent/loop.py`、`tool.py`、必要时 `skills/policy.py`、`tests/test_subagents_tool.py`、`tests/test_agent_loop_subagents.py`。
**实现动作：** CLI 构造好的 AgentTool 注册一次并在 Plan/Do 可见；标记非只读使 Scheduler 串行；mode/Skill visibility 复核后才由 PermissionManager/executor 调用。
**对应测试：** tool 固定、计划/执行模式、permission deny/confirmation、ToolScheduler serial、未知/隐藏调用无 worker。
**完成判定：** 任一拒绝先于 task creation，父 AgentLoop 不崩溃。
**禁止边界：** 不修改 Provider、ToolScheduler 与 Permission 语义；child registry 排除 agent。

### T16 — 主请求安全点结果投递

**前置条件：** T8、T15。
**允许文件：** `agent/loop.py`、`manager.py`、`tests/test_agent_loop_subagents.py`。
**实现动作：** 每个主请求最终 messages 构造前按 session scope drain 完成 notice，原子 claim 后以 assistant 背景摘要添加到主 session。
**对应测试：** worker 不持有 session、safe-point-only mutation、顺序、不丢失/不重复、foreground collect 不重复、旧 session 不串线。
**完成判定：** 通知只进入正确 parent ChatSession，单条 ≤4,000 字符且已脱敏。
**禁止边界：** 不在后台线程/ToolScheduler worker 写主历史，不回灌完整 child messages。

### T17 — 动态 Agent catalog

**前置条件：** T4、T15。
**允许文件：** `prompt/modules.py`、`agent/loop.py`、`tests/test_prompt_subagents.py`。
**实现动作：** 动态背景只包含 catalog 的 name+description 和固定提示；不同 Agent body、路径、digest/model/完整 metadata 不注入。
**对应测试：** 排序、覆盖后目录稳定、无定义/无效定义、敏感或路径字段不泄露。
**完成判定：** Parent 能选 definition，system prompt 不暴露正文。
**禁止边界：** 不把 prompt/catalog 写入 ChatSession/JSONL/Memory/artifact。

### T18 — Hook 占位决策和递归防线

**前置条件：** T14–T17。
**允许文件：** `tests/test_subagents_tool.py`、必要时 `tests/test_hooks_actions.py`。
**实现动作：** 验证 Hook `subagent` action 继续返回 `hook_subagent_not_available`，不调用 manager、不排队；AgentTool child 不包含 AgentTool/HookEngine。
**对应测试：** placeholder 零创建、child 无 Hook 回调、agent 嵌套请求拒绝。
**完成判定：** Chapter12 Hook 行为兼容且不递归。
**禁止边界：** 不激活 Hook SubAgent action，不改 Hook 规则/action 实现。

**Phase 4 退出门：** compileall 通过；`pytest tests/test_subagents_tool.py tests/test_agent_loop_subagents.py tests/test_prompt_subagents.py -q -rs --basetemp "$env:TEMP\newcode-pytest-ch13-p4"` 全通过；AgentTool/通知/gates 有证据。

## Phase 5：CLI 与 session lifecycle（T19–T21）

### T19 — CLI 注入 ProviderFactory 与单 Manager

**前置条件：** Phase 4 通过。
**允许文件：** `newcode/cli.py`、`tests/test_cli_subagents.py`。
**实现动作：** MCP discovery 后构造 Agent catalog、ProviderFactory 和每进程单 SubAgentManager；Factory 仅声明 `config.model`、为 child 建独立 client 且有限请求 timeout；创建并注册单个 agent Tool。
**对应测试：** 当前模型继承、可用性集合、独立 fake client、无额外工具、startup fail isolation。
**完成判定：** CLI 注入完全来自现有 config/Registry，不修改 config/Provider 文件。
**禁止边界：** 不读取 API key 到 Agent prompt/diagnostics，不对模型名静默 fallback。

### T20 — 新旧 session scope 切换和最终清理

**前置条件：** T19。
**允许文件：** `newcode/cli.py`、`tests/test_cli_subagents.py`，必要时既有 `tests/test_cli_session.py`。
**实现动作：** `/clear`、`/resume` 在 session 切换前取消旧 tasks、清通知并 bounded wait；EOF、exit、KeyboardInterrupt、异常和正常退出统一 close；SubAgent cleanup 在既有 Hook/Memory/Context/MCP cleanup 前完成且总等待 ≤1 秒。
**对应测试：** 各退出事件顺序、服务 cleanup 失败隔离、scope generation 更新、archive/resume 不恢复 task。
**完成判定：** 旧任务不能通知新 session，SubAgent shutdown 不阻塞后续 cleanup。
**禁止边界：** 不新增 slash command，不改原 `/clear`、`/resume`、Hook/Memory/Context/MCP 语义。

### T21 — CLI 受控端到端

**前置条件：** T14–T20。
**允许文件：** `tests/test_cli_subagents.py`、必要时最小 `cli.py` 修正。
**实现动作：** 用 FakeProvider、临时 workspace/home、fake clock/provider factory 驱动 Definition/Fork、background 完成通知和退出清理。
**对应测试：** 配置发现→普通 AgentTool→child fake 工具→safe result→后台通知→clear/resume/exit；不产生真实 API 调用。
**完成判定：** 主/子历史隔离、task 非持久化、结果一次交付、旧 CLI 命令回归。
**禁止边界：** 不用真实 Provider、网络、secret、第三方 MCP、tmux。

**Phase 5 退出门：** compileall 通过；`pytest tests/test_cli_subagents.py tests/test_cli_session.py tests/test_cli_hooks.py tests/test_cli_context.py tests/test_cli_memory.py tests/test_cli_mcp.py tests/test_cli_commands.py -q -rs --basetemp "$env:TEMP\newcode-pytest-ch13-p5"` 全通过。

## Phase 6：最终回归与验收（T22–T24）

### T22 — Chapter 4–12 与静态边界回归

**前置条件：** Phase 1–5 全通过。
**允许文件：** 默认只读；若直接证明回归，最小相关模块和对应测试。
**实现动作：** 执行 Provider/AgentLoop/Tools/Permission/Plan-Do/Context/Memory/MCP/Skill/Hook/Commands/CLI targeted tests；静态检查 Provider 无 SubAgent import、child 无嵌套/Hook、Tool gate 与 session isolation。
**对应测试：** 项目中 Chapter 4–12 全部相关测试及新的 SubAgent tests。
**完成判定：** 原有工具六件套、MCP、权限、上下文和命令语义无回归。
**禁止边界：** 不做与失败无直接因果的重构。

### T23 — 全量验证与 skip 审核

**前置条件：** T22。
**允许文件：** 默认只读；最小 regression fix + 测试例外同 T22。
**实现动作：** 全量 compileall、pytest `-rs` 与 `git diff --check`；记录每个 skip 的平台/环境原因。
**对应测试：** 全部 pytest。
**完成判定：** 无失败；所有 skip 有说明；diff check clean。
**禁止边界：** 不隐藏 skip、不使用外网、不安装 tmux。

### T24 — Fake CLI 验收与边界报告

**前置条件：** T23 全通过。
**允许文件：** 默认只读。
**实现动作：** fake provider/fixture 验收 definition/fork、permission deny、Plan/Do、foreground/background 三路径、通知领取、timeout/cancel、会话切换和 cleanup。
**对应测试：** 自动化 E2E 加静态审计清单。
**完成判定：** 提供每项日志/断言证据、skip 原因、未完成项及本章边界结论。
**禁止边界：** 不运行真实 API/MCP/Hook HTTP，不提交或暂存。

**Phase 6 退出门：** `.venv\Scripts\python.exe -m compileall newcode`、`.venv\Scripts\python.exe -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter13-final"`、`git -c safe.directory=F:/agent/Newcode diff --check` 均通过；受控 CLI fixture 完成。

## 顺序

```text
T1 → T2 → T3 → T4
                 ↓
T5 → T6 → T7 → T8
                 ↓
T9 → T10 → T11 → T12 → T13
                           ↓
T14 → T15 → T16 → T17 → T18
                           ↓
T19 → T20 → T21 → T22 → T23 → T24
```
