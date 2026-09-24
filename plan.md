# Chapter 13：SubAgent 实施计划

## 架构概要

主 Agent 继续由现有 AgentLoop 驱动；新增 `newcode/subagents/` 负责 definition discovery、窄权限策略、进程内任务状态、有界 worker、ProviderFactory 与统一 `agent` Tool。`agent` Tool 由主 CLI 在 MCP discovery 后注册一次，并作为非只读工具通过 AgentLoop → mode/visibility → PermissionManager → ToolScheduler → executor。子 Agent 使用独立 AgentLoop/ChatSession/Context/权限会话状态，但其有效工具集合只是父级能力的子集。

任务 worker 只写 SubAgentManager 的锁保护记录和有界通知队列。父 ChatSession 只由主 AgentLoop 在请求前安全点更新。CLI 负责创建/切换 session scope 与取消/关闭 manager；不新建 slash command，不让 Provider、MCP、Permission 或 ToolScheduler 理解 SubAgent。

## 核心数据与接口

- `AgentDefinition`：已校验名称、说明、source、正文、allow/deny、model、max_iterations、permission_mode、摘要/digest。
- `SubAgentTask`：task ID、父 session scope、definition/fork 类型、状态、执行方式、不可变输入快照、budget/stat、终态码、安全摘要、创建/完成序号、结果领取状态、取消 token。Manager 最多保留 128 条；仅淘汰最旧的已领取终态记录，未领取结果永不淘汰。
- `ParentPolicySnapshot`：不可变地记录启动时父 Permission policy 与有效可见工具上限；主线程在父策略收窄时发布新快照，Manager 按 session scope 保存最新版本，worker 只能读取 Manager 快照，不能访问父 AgentLoop/ChatSession。
- `SubAgentManager`：`start`、`status`、`wait`、`background`、`cancel`、`collect`、`drain_notifications(scope)`、`close_session(scope)`、`shutdown(deadline)`。所有状态跃迁、领取和通知标记串行化。
- `ProviderFactory`：报告当前可用 model，并按 model/请求 timeout 创建新的 ChatProvider；CLI 只声明配置 model 可用。child 不共享主 Provider client。
- `AgentTool`：固定 `agent` schema，将参数校验后委派 Manager；不创建动态 Tool。
- `SubAgentRunner`：创建 child scope、session、ContextManager、PermissionManager、ToolRegistry view、Skill state、budget/cancel state；只返回安全结果。

## 文件与职责

```text
newcode/subagents/
  __init__.py       公共窄接口
  types.py          definition、task、状态、预算和诊断纯类型
  discovery.py      定义文件扫描、优先级、YAML/frontmatter 与路径安全
  policy.py         父/角色/mode/background 工具集合的交集
  budget.py         单任务 token 近似计数、轮次与时限
  manager.py        有界队列、worker、状态机、通知/领取/取消/关闭
  runner.py         Definition/Fork child AgentLoop 构造与安全收尾
  tool.py           唯一固定 agent Tool 与稳定 schema
newcode/agent/loop.py           主请求安全点取通知、注册/执行控制 Tool 的接缝
newcode/prompt/modules.py       仅 name/description 的定义目录动态背景
newcode/cli.py                  ProviderFactory、Manager/Tool 注册、session 与退出清理
newcode/skills/policy.py        若需，在现有纯策略层暴露父 Skill 有效工具快照
tests/test_subagents_*.py       types/discovery/policy/manager/runner/tool 单元测试
tests/test_agent_loop_subagents.py
tests/test_prompt_subagents.py
tests/test_cli_subagents.py
```

不修改 Provider 接口或实现；CLI 的 ProviderFactory 在当前 config model 上创建独立 DeepSeekProvider/OpenAI client，并为 child client 设有限请求 timeout。现有配置文件、依赖、Permission 规则语义与 MCP runtime 不改。插件发现只接受可信调用方显式传入的资源目录，不导入插件代码；当前 CLI 的插件根集合为空。

## 数据流

1. CLI 加载配置与 MCP tools，构造 Registry；发现并校验 Agent 定义，校验其工具引用 against 最终 Registry。
2. CLI 创建 session scope、SubAgentManager 与 AgentTool，并将 AgentTool 固定注册为串行工具；PromptBuilder 只注入 agent name/description 目录。
3. 主 Agent 请求固定 `agent` schema 的某一 operation；`start` 的 Definition/Fork 字段组合由同一 schema 的严格校验器判定。主 AgentLoop 先按当前 mode/Skill/tool visibility 检查，再通过 PermissionManager；获准后 ToolScheduler 串行调用 AgentTool。
4. Manager 创建不可复用 task ID、冻结不可变 parent policy 上限与输入快照，放入有界队列，返回 task ID。Definition 必须使用 frontmatter 的 `tools.allow`（空列表为空普通工具集）；Fork 默认使用启动时父有效可见工具快照，请求 allowlist 仅进一步收窄。Worker 创建隔离子环境并运行既有 AgentLoop。
5. 父主线程在 mode、Skill whitelist、Permission 或其他有效可见范围收窄时发布新的不可变策略快照。child 每轮模型请求前及每次工具调用前通过 Manager 读取最新快照，并与启动上限相交；不得从 worker 直接读取父 AgentLoop/ChatSession 或可变对象。每个工具仍经过 child mode/visibility、独立 PermissionManager、ToolScheduler、executor/sandbox/MCP 路径。
6. child 终态摘要脱敏并限长，Manager 在锁内更新状态。foreground 通过 wait/collect 单次领取；background 形成有界完成通知。
7. 主 AgentLoop 在下一次主请求构造前按 session scope 安全取通知，按完成序写入 parent session；旧 scope 通知被丢弃，worker 不接触父 session。
8. CLI 在 clear/resume/退出时撤销 scope，取消任务并限时等待；child worker 自己 finally 清理 child Context artifact；其他既有 cleanup 仍继续。

## Phase 拆分与安全门

### Phase 1（T1–T4）：Definition 模型、发现与目录

**目标：** 定义 frontmatter、路径安全、来源优先级、同名覆盖和安全诊断，不运行 child。

**依赖：** 已批准 Chapter 13 spec；复用现有 Skill discovery 的 Markdown/YAML/symlink 经验与 package resource 布局。

**涉及文件：** `newcode/subagents/__init__.py`、`types.py`、`discovery.py`；`tests/test_subagents_types.py`、`tests/test_subagents_discovery.py`。

**风险/安全门：** 插件根不得通过扫描任意系统目录发现；无效高优先级文件必须容错而非遮蔽有效低优先级文件；名字/工具/model 不得来自执行中的动态代码。

**退出标准：** project/user/builtin/显式 plugin precedence 与 duplicate behavior 可重复；坏文件单独隔离；link/path escape 拒绝；正文不会被 startup catalog 暴露。

### Phase 2（T5–T8）：Task model、状态机与有界 manager

**目标：** 稳定 ID、生命周期、队列、后台切换、wait/collect、once-only delivery、预算边界和关闭；task record 总数最多 128。

**依赖：** Phase 1 的纯类型与安全错误码。

**涉及文件：** `types.py`、`budget.py`、`manager.py`；`tests/test_subagents_manager.py`、`tests/test_subagents_budget.py`。

**风险/安全门：** 锁内状态转换/结果领取；后台通知不能丢/重复/串 session；队列满立即安全失败；daemon worker 退出 deadline 有界，worker 异常不可逃逸。

**退出标准：** 状态转换表、并发/队列/session/record 上限、终态记录淘汰、轮次/超时/token budget、cancel、explicit/automatic/manual background、result claim 与 restart 非持久化均有 fake-clock/thread tests。

### Phase 3（T9–T13）：Policy、Definition/Fork runner 与隔离预算

**目标：** child AgentLoop 安全运行；Definition SOP、Fork snapshot、模型工厂、文件 cache、Permission/MCP/Skill/Context/Memory 策略齐全。

**依赖：** Phase 1 definitions，Phase 2 manager API。

**涉及文件：** `policy.py`、`runner.py`、`budget.py`、必要时 `skills/policy.py`；`tests/test_subagents_policy.py`、`tests/test_subagents_runner.py`。

**风险/安全门：** 工具集合只收窄；deny 优先；没有 HITL；Fork 不复制工具/system/dynamic 敏感内容；provider model 不 fallback；child 不写主历史、不共享 Memory/Context/Skill/Hook 状态；cache 前仍做 Permission。

**退出标准：** Definition 必填 allow/空集语义、Fork 默认父快照/请求 allowlist 收窄、permission trace、非交互拒绝、文件 cache 隔离/失效、token usage、Context artifact finally、MCP gate 与 background read-only gate 全部经本地 fake 验证。运行中的 child 在父 Do→Plan 及 Skill whitelist 收窄后，下一轮模型工具集合与下一次工具调用均体现收窄；父策略更新不可扩权，session 关闭会撤销快照并取消 child。

### Phase 4（T14–T18）：统一 Agent Tool 与 AgentLoop 安全集成

**目标：** 固定 schema、Plan/Do 可见、串行调度、Permission 链、主请求安全点通知与 dynamic catalog。

**依赖：** Phase 2 manager 和 Phase 3 runner/policy。

**涉及文件：** `tool.py`、`__init__.py`、`agent/loop.py`、`prompt/modules.py`、必要时 `skills/policy.py`；`tests/test_subagents_tool.py`、`tests/test_agent_loop_subagents.py`、`tests/test_prompt_subagents.py`。

**风险/安全门：** 工具名及完整字段 schema 固定；六个 operation 的字段组合和稳定错误码经过契约测试；AgentTool 非只读；child 无 agent Tool；Plan child read-only；Agent 调用不可绕过 Permission；通知只在 AgentLoop 主线程、主请求构造前写历史。

**退出标准：** 普通工具 call observation、deny/result ordering、hook recursion guard、背景通知 once/order/session binding、无配置默认行为，以及固定 schema 下所有 start/status/wait/background/cancel/collect 组合与错误行为均通过 fake AgentLoop/Tool 测试。

### Phase 5（T19–T21）：CLI、session scope 与资源清理

**目标：** CLI 构造 ProviderFactory/Manager，注册单工具；clear/resume/所有退出路径取消任务；Hook placeholder 维持不变。

**依赖：** Phase 4 AgentLoop integration。

**涉及文件：** `cli.py`、必要时 `agent/loop.py`；`tests/test_cli_subagents.py`、`tests/test_cli_hooks.py`（仅验证 placeholder 不变）、必要时既有 CLI tests。

**风险/安全门：** 不改 slash 命令语义；新 session generation 不复用；MCP/Hook/Memory/Context cleanup 不被阻塞；无真实网络或凭据。

**退出标准：** CLI 子 Agent E2E、model unavailable/no fallback、session switches、异常/EOF/exit/KeyboardInterrupt cleanup 与全局服务清理顺序均有自动化证据。

### Phase 6（T22–T24）：Chapter 回归、静态审计与最终验收

**目标：** 完成 Chapter 4–12 targeted regression、全量 compileall/pytest、静态边界核对和本地 fake E2E。

**依赖：** Phase 1–5 全部通过。

**涉及文件：** 默认不修改文件；若回归失败，仅允许最小相关生产改动及对应测试。

**风险/安全门：** 不因 full-suite skip 隐去平台限制；不跑真实 Provider/网络/MCP；不得顺手重构历史章节。

**退出标准：** Chapter checklist 全部有命令/fixture 证据；所有 skip 明确原因；`git diff --check` clean；工作树变更只属于 Chapter13。

## 风险与决策记录

| 风险/问题 | 已定决策 |
|---|---|
| 当前 Provider 没有 model catalog/cache contract | 仅当前配置 model 可用；无 cache 能力声明则普通请求运行，不承诺缓存/费用 |
| 同步 Provider call 难以强制中断 | child client 请求 timeout ≤30 秒；task 总 deadline 300 秒；manager 只等待有界期限 |
| session message 是可变列表 | child 使用脱敏不可变快照并构造独立 ChatSession |
| Skill/Hook 可能引入额外状态/递归 | 不继承或激活 parent Skill；不把 HookEngine 传入 child；Hook subagent 本章仍 unavailable |
| 父级策略在 child 运行中收窄 | 主线程发布不可变策略快照；Manager 是 worker 唯一读取入口；每轮和每工具调用复核并与启动上限取交集；不向父级扩权 |
| 工具 allowlist 空值语义 | Definition `tools.allow` 必填且空列表表示无普通工具；Fork 缺省继承父启动快照，显式列表（包括空列表）只收窄；deny 缺省为空且优先 |
| 固定 Agent Tool 多操作字段歧义 | 单一固定 schema 允许所有六种 operation 字段，但运行时严格校验 operation 专属必填/可选组合；非法组合统一 `subagent_invalid_request` |
| MCP adapter/同 server 并发 | 复用现有 MCPManager server lock；child Permission 仍 fail-closed |
| 共享 workspace 修改冲突 | 明确没有文件隔离，写入冲突不保证自动合并；不引入 Worktree |
| 当前无插件子系统 | 插件只经显式注入的可信资源根，默认 CLI 不加载外部插件 |

## 验收和回归命令

每个 Phase 先运行 `.venv\Scripts\python.exe -m compileall newcode`，再运行 checklist 对应 targeted pytest，测试通过才进入下一 Phase。最终运行：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter13-final"
git -c safe.directory=F:/agent/Newcode diff --check
```

`.venv` 不存在时记录并使用当前 Python；不安装 tmux 或其他依赖。人工/CLI 验收仅用 fake Provider、临时 home/workspace 与本地 fixture。
