# Chapter 13：SubAgent 验收清单

> 按 Phase 顺序执行。每个 Phase compileall 与 targeted pytest 全通过才可进入下一 Phase；任何失败、真实外网需求或范围外改动立即停止。所有测试使用 fake Provider/ProviderFactory、临时 home/workspace 与本地 fixture。

## Phase 1 — Definition 模型与发现（T1–T4）

- [ ] **T1 纯模型。** 前置：spec 批准；文件：`newcode/subagents/types.py`、`__init__.py`、`tests/test_subagents_types.py`。核对：字段、名称 `[a-z][a-z0-9-]{0,63}`、max iterations 1–8、未知/重复字段、稳定安全错误码。禁止读写文件、调用模型或启动 worker。证据：边界测试断言 validation code，repr 不包含 SOP。
- [ ] **T2 路径发现。** 前置：T1；文件：`discovery.py`、`tests/test_subagents_discovery.py`。核对：project/user/builtin/显式 plugin roots、仅普通 Markdown、根内解析、symlink/`..`/绝对逃逸拒绝。禁止遍历任意系统根、执行 plugin code。证据：临时目录和符号链接 fixture；不支持 symlink 的平台须 skip 并记明原因。
- [ ] **T3 Frontmatter 校验。** 前置：T1–T2；文件：`types.py`、`discovery.py`、两项 discovery/type tests。核对：`tools.allow` 必填且为字符串列表，空列表表示无普通工具；`tools.deny` 可选、缺省为空且 deny 优先；其余精确字段、正文≤64 KiB、单文件错误隔离、同源 duplicate 冲突隔离。禁止把 Markdown/SOP 当代码执行。证据：allow 缺失/空列表/错误类型、deny 缺省/优先级、未知字段、超长正文和 sibling isolation 测试通过。
- [ ] **T4 覆盖与目录投影。** 前置：T1–T3；文件：`discovery.py`、`tests/test_subagents_discovery.py`。核对：project > user > builtin > plugin、非法高层回退、工具引用针对最终 Registry 校验、只有 name/description 可用于启动目录。禁止模型目录泄露 body/path/digest/model/tools。证据：冲突矩阵和输出字段精确断言。

**Phase 1 退出门**

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_subagents_types.py tests/test_subagents_discovery.py -q -rs --basetemp "$env:TEMP\newcode-pytest-ch13-p1"
```

两条命令通过；若 symlink skip，记录 Windows 权限/平台原因；否则不得进入 Phase 2。

## Phase 2 — Manager、状态、预算和通知（T5–T8）

- [ ] **T5 状态与预算。** 前置：Phase 1；文件：`types.py`、`budget.py`、`tests/test_subagents_budget.py`、`tests/test_subagents_types.py`。核对：状态转换、ID/scope、8 轮/300 秒/16K近似token、输入/输出统计、最多128条记录及稳定上限错误。禁止伪造美元成本、持久化任务。证据：fake clock/token 和非法跃迁断言。
- [ ] **T6 有界 worker。** 前置：T5；文件：`manager.py`、`types.py`、`tests/test_subagents_manager.py`。核对：2 workers、4 queue、每 session 6 active/uncollected、128 records；启动快速返回 ID；满载安全拒绝；已领取终态旧记录可淘汰，未领取结果不丢。禁止无界线程/队列。证据：并发峰值和 queue/store full code 断言。
- [ ] **T7 操作与状态机。** 前置：T5–T6；文件：`manager.py`、`tests/test_subagents_manager.py`。核对：status/wait/background/cancel/collect；wait≤30秒，超时进入后台；显式后台 start、自动后台、父 Agent 手动 background；完成/取消 race 和单次 claim。禁止通过 CLI/HITL 自动批准或新增 slash command。证据：所有合法转换和重复领取测试通过。
- [ ] **T8 Session scope 与关闭。** 前置：T6–T7；文件：`manager.py`、`types.py`、`tests/test_subagents_manager.py`。核对：`(session ID,generation)` 绑定；按完成顺序通知；通知与 collect 互斥；close 清队列/取消旧任务；shutdown 合计≤1秒；重启无旧任务。禁止 worker 改父 ChatSession。证据：跨 session/generation 不投递、通知不重不丢、bounded wait 测试通过。

**Phase 2 退出门**

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_subagents_budget.py tests/test_subagents_manager.py -q -rs --basetemp "$env:TEMP\newcode-pytest-ch13-p2"
```

全部通过且有 concurrency/queue/timeout/shutdown 实测断言才可进入 Phase 3。

## Phase 3 — 权限策略与 Definition/Fork child（T9–T13）

- [ ] **T9 工具集合与实时父策略。** 前置：Phase 2；文件：`policy.py`、`manager.py`、必要时 `skills/policy.py`、`tests/test_subagents_policy.py`、`tests/test_subagents_manager.py`。核对：Definition 必填 allow（空列表为空集）；Fork allowlist 缺省继承启动时父有效可见工具快照，显式列表仅收窄；Registry ∩ 启动父 Permission/可见上限 ∩ Manager 最新不可变策略快照 ∩ role allow − deny ∩ child mode ∩ background readonly；deny 优先。父主线程在 mode/Skill/Permission/可见范围收窄时发布快照；worker 不读父 AgentLoop/ChatSession。每轮和每个工具调用前都复核，更新只能收窄。禁止把 visibility 当 Permission 或通过父扩权扩大 child。证据：Definition/Fork 空值语义；运行中 Do→Plan、Skill whitelist 收窄后下一轮 schema 和工具调用均排除被移除工具；session close 撤销快照并取消 child。
- [ ] **T10 Child registry/Permission。** 前置：T9；文件：`runner.py`、`policy.py`、`manager.py`、`tests/test_subagents_policy.py`、`tests/test_subagents_runner.py`。核对：独立 Permission state；不继承 session allow；confirmation 用 deny-by-default；hard deny/sandbox 仍优先；无 agent/load_skill 子工具；child 从 Manager 取得最新不可变策略而不读取父可变对象。禁止自动确认、工具直调绕过。证据：deny/confirmation 时 executor 调用数为零；父策略收窄后模型可见集合与工具调用均收窄。
- [ ] **T11 Definition runner。** 前置：T10；文件：`runner.py`、必要时 `types.py`、`tests/test_subagents_runner.py`。核对：SOP 每轮 system context、child session/loop/context/usage/cancel/Skill state 独立、无 Memory/Hook/archive、Context artifact finally 清理。禁止写主 session 或共享会话状态。证据：成功、异常、取消皆 cleanup，parent history 不变。
- [ ] **T12 Fork 与模型/cache。** 前置：T11；文件：`runner.py`、`types.py`、`tests/test_subagents_runner.py`、必要时 policy tests。核对：快照最多20条纯 user/assistant、总长≤12,000，脱敏、不可变；无工具调用/结果、system/dynamic、Skill SOP、Hook、Memory；冻结启动时父 Permission/工具上限；Fork allowlist 缺省继承该快照、显式列表只收窄；unsupported model 无 fallback；cache 无能力声明时安全普通运行。禁止假定 haiku/sonnet/opus 可用或声称省费。证据：0/20 边界、快照隔离、`deepseek-v4-pro` 当前 model factory 与 unsupported model 测试。
- [ ] **T13 Cache/MCP/Skill/预算。** 前置：T9–T12；文件：`runner.py`、`budget.py`、必要时 `policy.py`、runner/policy tests。核对：child-local read cache≤128项/2MiB、stat 校验、写/command/MCP 后失效，cache hit 前仍过 Permission；MCP 经现 manager；background 只读；Context/MCP/Skill 不共享会话状态。禁止跨 sandbox 读、记忆提交或 Worktree。证据：cache miss/hit/invalidation、MCP confirmation fail-closed、超限终止测试。

**Phase 3 退出门**

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_subagents_policy.py tests/test_subagents_runner.py -q -rs --basetemp "$env:TEMP\newcode-pytest-ch13-p3"
```

工具交集、fork privacy、non-interactive permission 与 child cleanup 全通过才可进入 Phase 4。

## Phase 4 — 统一 Tool 与 AgentLoop/Prompt（T14–T18）

- [ ] **T14 固定 Agent Tool。** 前置：Phase 2–3；文件：`tool.py`、`types.py`、`tests/test_subagents_tool.py`。核对：全局仅一个固定 `agent` 名/schema；六个 operation 字段、必填/可选项和合法组合固定。start 必填 kind/task_prompt/execution；Definition 必填 agent_name 且禁止 model_override/allowlist；Fork 禁止 agent_name、可选 model_override/allowlist；其余操作必填 task_id，wait_seconds 仅 wait 可用。确认 status 不领取、wait 限时/超时转后台、background/cancel 状态语义、collect 单次领取，以及稳定错误码。禁止动态工具注册/schema或 Tool 直接调用 Provider/UI。证据：schema snapshot、所有合法/非法组合、状态/领取与错误码测试通过。
- [ ] **T15 AgentLoop gate。** 前置：T14；文件：`agent/loop.py`、`tool.py`、必要时 `skills/policy.py`、AgentLoop/tool tests。核对：Plan/Do 可见性、非只读串行调度、mode/Skill visibility 后 permission/executor 才运行。禁止修改 Provider/Permission/ToolScheduler 语义或 child recursion。证据：deny/confirmation/Plan/Do 时 task manager 创建数为零或严格受限。
- [ ] **T16 主请求安全点通知。** 前置：T8、T15；文件：`agent/loop.py`、`manager.py`、`tests/test_agent_loop_subagents.py`。核对：主请求构造前 drain，只有 AgentLoop 主线程写 parent session，顺序、去重、session binding、≤4,000字符 redacted。禁止 worker 线程写 session。证据：同步/并发 fixture 核对历史变更点与调用顺序。
- [ ] **T17 动态 Agent 目录。** 前置：T4、T15；文件：`prompt/modules.py`、`agent/loop.py`、`tests/test_prompt_subagents.py`。核对：catalog 仅 name+description，稳定排序；body/model/path/digest/tools 不泄露，不持久化动态目录。证据：prompt content 精确字段与敏感 sentinel absence。
- [ ] **T18 Hook placeholder/递归。** 前置：T14–T17；文件：tool tests，必要时 `tests/test_hooks_actions.py`。核对：Hook `subagent` 保持 `hook_subagent_not_available`，manager 零调用；child 无 HookEngine 和 agent Tool。禁止激活 Hook action。证据：零 task 创建/零 worker 排队。

**Phase 4 退出门**

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_subagents_tool.py tests/test_agent_loop_subagents.py tests/test_prompt_subagents.py -q -rs --basetemp "$env:TEMP\newcode-pytest-ch13-p4"
```

确保 agent Tool 始终经 Permission、ToolScheduler 与 executor，后台不直接改 session。

## Phase 5 — CLI/session 生命周期（T19–T21）

- [ ] **T19 CLI wiring。** 前置：Phase 4；文件：`newcode/cli.py`、`tests/test_cli_subagents.py`。核对：MCP discovery 后构建 catalog、one manager、ProviderFactory、one AgentTool；当前仅声明 config.model 可用，child client 请求 timeout 有界。禁止改 config/Provider、打印 secret、model fallback。证据：fake factory 注入和工具数量/schema 测试。
- [ ] **T20 Session transitions/shutdown。** 前置：T19；文件：`cli.py`、`tests/test_cli_subagents.py`、必要时 `tests/test_cli_session.py`。核对：`/clear`、`/resume` 取消旧 scope；EOF/exit/KeyboardInterrupt/异常/正常关闭 aggregate wait≤1秒，然后仍执行 Hook→Memory→Context→MCP cleanup。禁止新增 slash command、task archive 或 cleanup 短路。证据：cleanup order、每资源一次、resume 不恢复 task 的测试。
- [ ] **T21 Fake E2E。** 前置：T14–T20；文件：`tests/test_cli_subagents.py`、仅失败直接相关的最小 CLI 修复。核对：definition/fork、background result、领取通知、timeout/cancel/clear/resume/exit。禁止真实 Provider/network/secret/third-party MCP/tmux。证据：parent/child history 分离、摘要一次交付、任务不可跨 session 查找。

**Phase 5 退出门**

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_cli_subagents.py tests/test_cli_session.py tests/test_cli_hooks.py tests/test_cli_context.py tests/test_cli_memory.py tests/test_cli_mcp.py tests/test_cli_commands.py -q -rs --basetemp "$env:TEMP\newcode-pytest-ch13-p5"
```

全路径 shutdown fail isolation、旧 slash 命令和 MCP 生命周期通过后才可最终回归。

## Phase 6 — 最终验收（T22–T24）

- [ ] **T22 Chapter 4–12 regression/static audit。** 前置：Phase 1–5；文件：默认只读，失败时仅最小相关文件与回归测试。核对：Provider 不导入 SubAgent；AgentTool/child 工具路径、Permission、Plan/Do、Skill、MCP、Context、Memory、Hook、Command 边界。禁止无因重构。证据：指定回归 pytest 和 import/调用链审计记录。
- [ ] **T23 Full suite。** 前置：T22；文件：默认只读。核对：全量 compileall、pytest `-rs`、diff check；每个 skip 写明原因。禁止隐藏 skip 或跨本章修改。证据：命令退出码、pass/skip 明细、clean diff check。
- [ ] **T24 CLI fake acceptance。** 前置：T23；文件：默认只读。核对：Definition/Fork、授权拒绝、Plan/Do、显式/自动/手动后台、通知、timeout/cancel、session 切换、cleanup 与 Hook placeholder。禁止真实 API/MCP/HTTP/secret/tmux。证据：fake fixture 端到端输出与未完成项列表。

**Phase 6 退出门**

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_provider_tool_calls.py tests/test_deepseek_provider.py tests/test_agent_modes.py tests/test_agent_scheduler.py tests/test_agent_loop.py tests/test_agent_loop_permissions.py tests/test_agent_loop_context.py tests/test_agent_loop_memory.py tests/test_agent_loop_mcp.py tests/test_agent_loop_hooks.py tests/test_tools_executor.py tests/test_tools_registry.py tests/test_permissions_manager.py tests/test_permissions_sandbox.py tests/test_context_manager.py tests/test_memory_service.py tests/test_mcp_manager.py tests/test_skills_runner.py tests/test_hooks_engine.py tests/test_commands_builtins.py tests/test_cli_commands.py tests/test_cli_session.py tests/test_prompt_builder.py tests/test_subagents_types.py tests/test_subagents_discovery.py tests/test_subagents_budget.py tests/test_subagents_manager.py tests/test_subagents_policy.py tests/test_subagents_runner.py tests/test_subagents_tool.py tests/test_agent_loop_subagents.py tests/test_prompt_subagents.py tests/test_cli_subagents.py -q -rs --basetemp "$env:TEMP\newcode-pytest-ch13-regression"
.venv\Scripts\python.exe -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter13-final"
git -c safe.directory=F:/agent/Newcode diff --check
```

三项命令均通过，skip 原因完整，受控 CLI 自动验收通过；否则停止并报告，不得宣称 Chapter 13 验收完成。
