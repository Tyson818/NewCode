# Chapter 13：SubAgent —— 子 Agent 与任务分发

## 1. 背景与当前实现

NewCode 当前通过 `ChatProvider.stream_chat(messages, tools, allow_tool_calls)` 提供统一的流式模型接口。运行时配置只有一个 `model` 字符串；仓库中的 `config.yaml` 使用 `deepseek-v4-pro`。当前 `DeepSeekProvider` 按配置字符串发请求，不声明可用模型目录、并发能力或 prompt-cache 控制能力；`newcode/prompt/cache.py` 只解析可能存在的 cache usage 元数据，不保证缓存能力或费用节省。

AgentLoop 已负责主会话消息、Plan/Do 可见性、Permission 检查、Hook、Skill 与 Context，并通过 ToolScheduler 和 executor 执行工具。默认工具为六个内置工具，CLI 还可能注册 MCP 工具；AgentLoop 会注册 `load_skill`。新增委派必须作为单一静态 Agent 工具走相同调用链，不能在 Provider 内编排子 Agent。Chapter 12 的 Hook `subagent` action 当前返回 `hook_subagent_not_available`。

## 2. 目标与固定决策

本章增加受限的定义式/Fork 式子 Agent、统一 Agent 工具、任务生命周期与后台结果通知。子 Agent 是普通 AgentLoop 实例的隔离运行，不是新的权限主体；其有效能力不得超过父 Agent 当前会话、模式与 Permission 所允许的范围。

固定资源上限：每个 CLI 进程最多 2 个并发子 Agent worker、最多 4 个排队任务、每个 ChatSession scope 最多 6 个未终结/未领取任务、manager 最多保留 128 条 task record；单任务最多 8 轮、300 秒、16,000 个近似输入加输出 token。等待单次最多 30 秒；CLI/session 关闭对所有 worker 使用合计不超过 1 秒的等待期限。任务结束摘要最多 4,000 字符。满载以稳定错误码结束或拒绝，不悄悄扩容。

Token 预算使用每次请求与流式文本的近似字符估算；若 Provider 可提供可信 usage，则同时记录实际 usage，但预算仍不能因缺失 usage 而失效。当前 DeepSeek 实现没有对外提供可靠 usage 事件，所以当前运行以估算为准；本章不估算货币成本、不声称省钱。

## 3. 统一 Agent 工具与生命周期

### F1：固定工具面

- 工具名称固定为 `agent`，只注册一个静态 Tool，不按 Agent/任务动态生成工具或 schema。
- Schema 在所有角色、任务和状态下保持同一份固定字段定义；`operation` 固定为 `start`、`status`、`wait`、`background`、`cancel`、`collect`。不按角色或任务动态增删字段或生成工具。
- 固定字段及组合如下：
  - 所有请求必填 `operation`。除 `start` 外，其他操作必填不透明 `task_id`，不得附带 start 字段；未知字段、缺字段、类型错误或不合法组合统一返回 `subagent_invalid_request`。
  - `start` 必填 `kind`、非空 `task_prompt` 和 `execution`；`execution` 只能为 `foreground|background`，不省略、不推断。`task_prompt` 受单任务输入预算限制，超限返回 `subagent_token_budget_exceeded`。
  - `kind=definition` 时必填 `agent_name`，禁止 `model_override` 与请求级 `allowlist`；模型只取定义 frontmatter 的可选 `model`，否则继承 CLI 当前模型。定义必须含 `tools.allow` 字符串列表；空列表表示无普通工具。`tools.deny` 可省略，省略等于空列表，deny 永远优先。
  - `kind=fork` 时禁止 `agent_name`；`model_override` 与 `allowlist` 可选。缺省 `allowlist` 使用启动时父级有效可见工具的不可变快照；提供时只与该快照取交集，空列表表示无普通工具，不能扩大权限。`model_override` 仅在 ProviderFactory 明确支持时可用，否则返回 `subagent_model_unavailable`，不回退。
  - `status` 只读并返回任务状态摘要，不等待、不领取；`wait` 可选 `wait_seconds`（整数 1–30，缺省 30），最多等待该时长，终结后原子领取结果，未终结则转 background 并返回状态/task ID；`background` 仅接受 queued/running 任务并幂等设置后台交付；`cancel` 对活动任务请求取消、对终结任务幂等返回终态；`collect` 仅领取终结且未交付结果一次。未完成的 collect 返回 `subagent_result_not_ready`，重复领取返回 `subagent_result_already_collected`，不存在或不属于当前 session 的 ID 返回 `subagent_not_found`。
- `start` 总是快速排队并返回 task ID，不在 Tool executor 内同步跑完整任务。foreground 表示结果需由 `wait`/`collect` 显式领取；background 表示完成后可在 AgentLoop 安全点自动通知。
- 父 Agent 可在任务运行期间显式调用 `background`，这是本章“手动切后台”的唯一触发者；无需新增 slash command。`wait` 超时切后台和显式 `background` 都保留结果通知/领取的单次互斥规则。

### F2：状态机与稳定结果

任务状态固定为 `queued`、`running`、`background`、`completed`、`failed`、`cancelled`、`timed_out`。合法转换：`queued → running|cancelled`；`running → background|completed|failed|cancelled|timed_out`；`background → completed|failed|cancelled|timed_out`。终结态不再转换。完成标记、取消、超时和结果领取必须在 manager 锁内原子处理；取消/超时以后到达的结果不得覆盖终结态。

错误码固定至少包括：`subagent_invalid_request`、`subagent_not_found`、`subagent_definition_invalid`、`subagent_model_unavailable`、`subagent_permission_denied`、`subagent_queue_full`、`subagent_concurrency_limit`、`subagent_task_store_full`、`subagent_timeout`、`subagent_cancelled`、`subagent_iteration_limit`、`subagent_token_budget_exceeded`、`subagent_provider_error`、`subagent_result_already_collected`、`subagent_parent_session_closed`。模型、工具或异常的原始文本不得成为错误码/诊断。

终态且已领取记录按完成时间先后淘汰以维持 128 条上限；未领取的终态结果不得淘汰。若记录数已满且没有可淘汰项，新的 start 返回 `subagent_task_store_full`。

task ID 使用安全随机值，仅在当前进程有效，不写入 session archive 的独立任务表，不跨进程恢复。主 Agent 的正常工具调用记录仍按既有 JSONL 语义保存；恢复历史中的旧 task ID 不能恢复或查询旧任务。

### F3：有界结果通知

- worker 绝不直接修改父 ChatSession。完成记录先存于有界 manager 状态/通知队列；任务总数上限确保通知满时结果仍可从终结记录领取，不丢失。
- AgentLoop 只在主请求构造前的安全点，按完成序号取出当前 session scope 的待通知结果，原子标记已投递，再追加脱敏、长度受限的 assistant 背景通知。`collect`/foreground `wait` 先领取的结果不会再通知。
- 单条通知包括 task ID、终态、安全错误码（若失败）和至多 4,000 字符摘要。超时/取消/异常的原始 stack、路径、凭据、header、env 与完整工具结果不通知。
- 所有任务记录绑定不可复用的 `(ChatSession ID, 本进程 session generation)`。不同 session 或 generation 的通知被丢弃；不同任务按完成序号投递，不按 worker 竞态写入历史。

## 4. 定义式 Agent

### F4：发现、覆盖和容错

单文件 Markdown + YAML frontmatter，正文是该定义式 Agent 每轮完整的角色 system SOP，不执行正文中的脚本。发现根固定为：项目 `.newcode/agents/`、用户 `~/.newcode/agents/`、包内 `newcode/resources/agents/`，以及由可信内部调用方显式传入的插件包资源根。优先级固定 `project > user > built-in > plugin`；名称使用 casefold 后的 `[a-z][a-z0-9-]{0,63}`。

同一来源的重复 name 视为该来源冲突并跳过冲突项；跨来源由高优先级有效项覆盖低优先级项。高优先级文件无效时只记录脱敏诊断并跳过，低优先级有效定义可胜出。单文件解析失败不阻断其他定义或 CLI。当前项目没有插件 manager；本章仅提供插件资源根注入点，CLI 默认不发现任意第三方目录、不导入插件代码。

严格校验字段：`name`、单行 `description`、必填的 `tools.allow` 字符串列表、可选的 `tools.deny` 字符串列表（缺省为空）、`model`、`max_iterations`、`permission_mode`。`tools.allow: []` 明确表示不允许普通工具，不等同于字段缺失；字段缺失或类型错误使定义无效。拒绝未知字段、重复工具名、未知工具引用、过长 frontmatter/正文、路径逃逸、符号链接和非普通文件。Agent 正文上限 64 KiB；每个无效定义只有安全诊断 code/source/name，不记录正文或路径凭据。

`max_iterations` 范围为 1–8，仍受进程硬上限 8 限制。`permission_mode` 为 `inherit|strict|default|permissive|trusted`；子 Agent 有效模式取父级当前模式与定义模式中更保守者，定义不能升权；strictness 顺序为 `strict < default < permissive = trusted`。Permission 显式规则、hard denylist 与 sandbox 仍由既有 PermissionManager 决定，定义文件不能覆盖它们。

## 5. Fork 式 Agent 与 Prompt Cache

### F5：不可变 Fork 快照

Fork 在启动时固定父 session 快照，只携带最近最多 20 条有正文的 user/assistant 普通文本消息，按原顺序复制，累计最多 12,000 字符；通过现有敏感值脱敏后创建独立 ChatSession。工具调用、工具结果、任何 system/dynamic prompt、已激活 Skill SOP、Hook injection、Memory 内容、Context artifact、父 Permission session state 和 pending task 通知一律不复制。Fork 的任务说明作为独立 user 内容，不拼接到 system instruction。父会话此后变化不影响快照。

定义式 Agent 同样拥有独立 session；其 system SOP 来自已验证定义正文，输入任务单独作为 user 消息。两种模式均只把安全最终摘要交给父 Agent，不共享消息列表。

### F6：模型选择与 Prompt Cache

默认继承本次 CLI 配置的 model。指定 model 必须由注入的 ProviderFactory 明确声明可用，并能为该 model 创建兼容 ChatProvider；不支持时返回 `subagent_model_unavailable`，不得静默 fallback。当前 DeepSeekProvider/配置没有模型目录或 override capability；当前 CLI 只声明配置中的 `deepseek-v4-pro` 可用，因此 `haiku`、`sonnet`、`opus` 等名称不被假定可用。

Prompt cache 属于 Provider 可选能力。当前接口无 cache 控制 contract；仅维持固定 system 前缀和稳定消息排序，不宣称启用缓存或节省费用。未来 capability 未声明时安全按普通请求运行；不得因 cache 缺失而破坏任务。

## 6. 子 Agent 隔离、工具与权限

### F7：每任务独立运行状态

每个任务拥有独立 ChatSession、AgentLoop/轮次计数、PermissionManager session state、Permission trace、文件读缓存、ContextManager/circuit/usage、取消 token、Skill activation state、预算统计和临时结果。没有跨会话或跨进程 task persistence，不共享 MemoryService。每个 child Context artifact 使用独立受控 session 子目录，并在 worker `finally` 清理；清理失败只记录安全诊断，不阻塞 CLI 其他服务清理。

ProviderFactory、只读 Tool 定义、MCPManager/transport 可共享的仅是无会话状态且线程安全的基础设施；Provider client 默认由 factory 为每个 child 创建独立实例，不假定 SDK client 并发安全。MCP 的 server lock 继续由 MCPManager 管理。HookEngine 不共享给 child，避免 Hook 循环。文件系统与 workspace 根按父 Agent 共享，本章不提供 Worktree/文件隔离；同文件并发修改可能覆盖彼此，用户/Agent 应避免并行写同一目标。

子 Agent 文件读缓存仅缓存已获 Permission/sandbox 允许的 `read_file` 结果，按解析后 workspace 相对路径与文件 stat 签名校验；每任务最多 128 项/2 MiB。write/replace/run_command/MCP 结果后失效；任何 cache hit 前仍执行 Plan/Do 与 Permission 检查，缓存不能绕过路径校验。

### F8：有效工具集合与非交互 Permission

普通 child 工具集合为以下集合的交集，再减去所有 deny。定义式 `tools.allow` 必填；空列表就是空普通工具集。Fork 的请求级 `allowlist` 可选：未提供时继承启动时父有效可见工具快照，显式空列表是空集，非空列表只进一步收窄该快照，不存在“未配置即无限制”的语义。

1. 当前 ToolRegistry 中实际存在的工具；
2. task 启动时父 Agent 的有效可见工具与权限上限快照；
3. 定义式必填 allow 或 Fork 的默认父快照/可选收窄 allowlist；
4. 启动后 Manager 最新父级策略快照所允许的工具；
5. 当前 child mode 可见集合；
6. 后台限制：background 状态只允许 `read_file/find_files/search_code`，禁止写工具、run_command 和 MCP。

deny 永远优先。父策略快照在 child 启动时记录父 Permission policy 与可见工具上限，均为不可变值。父主线程在 mode、active Skill whitelist、Permission 或其他有效可见范围收窄时，向 SubAgentManager 发布新的不可变策略快照；child 每轮模型请求前及每次工具调用前从 Manager 读取最新快照，并与启动上限、角色策略及其他限制取交集。该通道只可收窄，不能因父级后来扩权而扩大 child 启动上限。worker 不得读取父 AgentLoop、ChatSession 或其他可变父状态；session scope 关闭时 Manager 撤销策略快照并取消 child。

`agent` 不加入 child registry，禁止递归。child 不继承/激活 parent Skill；不注册 `load_skill`，所以 Skill SOP 与白名单不能由子 Agent扩大。工具可见不等于已授权：child 仍完整经过 AgentLoop mode/可见性复核、PermissionManager、ToolScheduler、executor、MCPManager 与 workspace sandbox。

每个 child 使用与父级同一配置源的项目/用户 Permission 规则，但不复制父 session allow。Permission 需要确认时使用非交互 deny-by-default confirmer，命令/工具零执行并产生安全 ToolResult observation。child Permission trace 仅存允许/拒绝 decision code、tool name 与时间，不存原始参数/secret。

## 7. 后台、预算、取消和关闭

### F9：后台任务

显式后台：`start.execution=background` 立即返回 ID。自动后台：foreground task 的 `wait` 达到设定等待上限仍未结束即转为 background。手动后台：父 Agent 对一个 running foreground task 调用 `background`。background child 下一次请求起应用只读工具限制；已进入 executor 的单个调用无法回滚，必须等待其工具 timeout 或 worker 取消点。

worker 使用固定 2 个 daemon worker 与最多 4 个排队槽；超过每 session 6 个活动/未收结果任务或队列满时安全返回 `subagent_concurrency_limit`/`subagent_queue_full`。每任务最多 8 轮、300 秒和 16,000 近似 token；到期通过取消 token 阻止下一次模型/工具调用并尝试关闭 Provider stream。当前同步 Provider 不承诺中断正在执行的网络调用；CLI 关闭等待合计最多 1 秒后安全放弃 worker，child 的 `finally` 在其返回后清理，不拖延 Hook/Memory/Context/MCP 清理。

### F10：CLI session 生命周期

每个 CLI 进程一个 SubAgentManager；每次新 ChatSession 获得新的不可复用 scope generation。`/clear`、`/resume` 先取消旧 scope 任务、清空其未投递结果并有限等待，再切换 session；新 scope 绝不接收旧通知。EOF、`/exit`、KeyboardInterrupt、异常和正常关闭均取消当前 scope，manager 全局 shutdown 使用合计 1 秒 deadline，随后继续既有 Hook → Memory → Context → MCP cleanup 次序。child state 从不写入 SessionArchive；恢复会话不会恢复旧 task。

## 8. Hook、Command 与现有功能兼容

本章不激活 Hook `subagent` action；它继续返回 `hook_subagent_not_available`，零调用 SubAgentManager、零排队任务。这样可避免声明式 Hook 递归/权限继承语义未充分定义。

Agent 只作为普通文本对话中的 AgentLoop Tool，不新增 slash command，不更改 Chapter 10 的固定十命令、别名、`/review`、Tab 补全或静态命令优先规则。`/clear` 和 `/resume` 只增加对旧 task scope 的取消，不改变现有 session/Context/Skill/Hook/MCP/Memory 生命周期。Context 对 child 是独立实例并可按现策略压缩；Memory 不共享也不提交 child 内容；MCP 仅作为 registry 中已发现且经权限过滤的工具；Provider 不理解任务、角色或 SubAgent。

Plan Mode 可见单一 Agent 工具，但 child 只能看到 Plan Mode 的只读工具，且仍经 Permission；Do Mode 受父当前可见集合约束。拒绝、Provider 错误、超限、取消与后台失败都转为正常脱敏 ToolResult/status，不抛出导致父 AgentLoop 崩溃的异常。

## 9. 敏感信息与错误模型

- Agent 定义路径、frontmatter、用户 Fork prompt、父快照、工具数据、Hook/Memory/Context 内容均不写入诊断；错误只使用稳定 code 和必要的安全名称。
- Fork snapshot、child final answer 与后台通知应用现有 sensitive-values redaction，并截断至相应上限；不回流完整工具结果、exception/stack、MCP 原始错误、API key、headers、env 或 URL credentials。
- task state、task queue、Permission trace、file cache 均为进程内数据；session archive 仅可能保存 AgentTool 的既有主会话 tool-call/result 及已投递的安全摘要，不保存可恢复任务状态。
- ToolResult 以现有失败结果约定回到主 Agent；before child 工具执行的所有安全门优先于定义/Fork 指令。任何内部错误不得隐式转 allow。

## 10. 非目标

- 不实现 Worktree、文件系统隔离、Agent Teams、SubAgent 递归、跨会话/跨进程任务持久化。
- 不修改 Provider orchestration contract；不假设 Haiku/Sonnet/Opus 当前可用，不保证 prompt cache 或费用节省。
- 不让 Hook 创建/调度子 Agent；不做模型动态安装、远程 Agent 下载、市场、任意插件代码执行、自定义 slash command。
- 不绕过 Plan/Do、Permission、hard denylist、workspace sandbox、ToolScheduler、MCP、Skill whitelist、Context 或 Memory 边界。
- 不使用真实第三方 MCP、真实网络凭据、生产 secret 或真实 Provider 的验收请求。

## 11. 验收标准与测试范围

AC1：统一 `agent` Tool 始终只有一个稳定名称/schema，六个 operation 的字段、必填/可选项、合法组合、状态/结果领取语义及错误码均可通过 schema/契约测试验证；所有 operation 均通过主 AgentLoop 的可见性与 Permission 检查；SubAgent tool 不进入 child registry，ToolScheduler 将它串行执行。

AC2：Agent 定义严格校验、项目/用户/内置/插件优先级与覆盖稳定；单文件错误隔离；路径逃逸/符号链接拒绝。

AC3：定义式及 Fork child 均使用独立 session/runtime；Fork 只带最多 20 条脱敏纯 user/assistant 文本且总计至多 12,000 字符；主 session 后续改动不影响 child。

AC4：定义式必须提供 `tools.allow` 列表（空列表为无普通工具），可选 deny 缺省为空且永远优先；Fork 缺省继承启动时父级有效可见工具快照，请求 allowlist 只收窄。有效工具集准确为全局、父启动上限、Manager 最新父策略快照、角色 allow/deny、child mode、后台限制的交集。父 Do→Plan 和 Skill whitelist 收窄后，运行中 child 下一轮看不到被移除工具；每次调用也复核最新快照。测试证明任何 allow 都不能越过 hard deny、sandbox、Permission confirmation、Plan 或 Skill 限制。

AC5：Haiku/Sonnet/Opus 等未被当前 ProviderFactory 声明的 model 安全失败、无 fallback；prompt cache 未声明时正常运行且不声称缓存命中/省费。

AC6：并发、队列、session task 上限、8 轮/300 秒/16K 近似 token 预算、状态转换、取消、超时、foreground wait、自动/显式/手动 background 均有 fake 驱动测试。

AC7：后台完成结果只由 AgentLoop 安全点投递，按完成序、不重复、不串 session；摘要脱敏且 ≤4,000 字符；collect 与自动通知互斥。

AC8：子 Agent Permission confirmation 非交互 fail-closed；MCP、Skill、Context、Memory、Hook 与 Command 集成均符合本章决策；Hook subagent placeholder 不创建 task。

AC9：CLI clear/resume/退出/异常取消任务并有限等待；SubAgent shutdown 不阻塞 Hook、Memory、Context、MCP cleanup；任务不跨 session 恢复。

AC10：所有单元/E2E 用 fake Provider、临时 home/workspace、本地 fixture；完成 Chapter 4–12 回归及全量 pytest/compileall/diff 检查，无真实网络、生产 secret 或第三方 MCP。
