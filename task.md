# Chapter 12：Hook 系统——原子任务

## T1：Hook 模型与安全错误码

- 前置条件：无。
- 允许文件：新增 newcode/hooks/__init__.py、types.py、tests/test_hooks_types.py。
- 实现：定义 stable ID、event/action、仅 before_tool 的规则级 deny、condition/context/outcome/diagnostic、once scope identity 和受限 timeout 校验。
- 测试：合法/非法 ID、event/action/deny、五种 scope key、once/async、诊断脱敏。
- 完成：纯数据、JSON-safe，不导入 Provider/MCP/执行层。
- 禁止：YAML、文件、网络、工具、注册或 action。

## T2：YAML loader、路径安全、合并

- 前置条件：T1。
- 允许文件：新增 hooks/loader.py，修改 hooks/__init__.py，新增 tests/test_hooks_loader.py。
- 实现：加载用户和项目 hooks.yaml，验证项目 sandbox/普通文件，逐条隔离，按未覆盖 user 后接 project 合并；只从用户文件读取 network.enabled/allow_hosts，拒绝项目 network 配置。
- 测试：缺文件、坏 YAML、缺字段、非法 event/action、重复 ID、覆盖、顺序、符号链接/逃逸、用户授权合并、项目不得启用 HTTP/增加 host。
- 完成：错误仅安全诊断，不接 CLI。
- 禁止：AppConfig、Permission loader、CLI 改动。

## T3：受限条件 matcher

- 前置条件：T1。
- 允许文件：新增 hooks/conditions.py，修改 types.py，新增 tests/test_hooks_conditions.py。
- 实现：allowlisted field 的 exact/glob/regex/not 与单层 all/any。
- 测试：每种匹配、混用拒绝、非法 regex、缺字段、不匹配 skip、Permission regression；pattern 上限 256 字符、目标字段上限 1024 字符；灾难性回溯形态拒绝/有界完成；预算超限仅拒绝对应匹配，AgentLoop 继续。
- 完成：无表达式或模板执行；匹配器有线性复杂度上界，不对不可信项目配置使用无界 Python re。
- 禁止：修改 Permission rules 语义。

## T4：引擎的顺序、once、递归抑制

- 前置条件：T1–T3。
- 允许文件：新增 hooks/engine.py，修改 types.py、__init__.py，新增 tests/test_hooks_engine.py。
- 实现：稳定 emit、once key、origin guard、safe diagnostics、同步 before_tool 规则级 deny outcome，命中 deny 时跳过该条 action；固定 process/session/turn/message/tool scope 映射。
- 测试：顺序、skip、system process ID、session ChatSession ID、turn session ID+index、message session ID+message ID、tool session ID+tool call ID；clear/new/resume reset；跨 CLI process 不恢复。
- 完成：引擎不实际执行 action，合法 deny 与失败可区分。
- 禁止：AgentLoop/CLI 接入和 ToolCall 参数改写。

## T5：Action runner 与 transient prompt queue

- 前置条件：T4。
- 允许文件：新增 hooks/actions.py、lifecycle.py，修改 engine.py/types.py，新增/修改 hooks action/engine tests。
- 实现：单 worker、有界队列、timeout、prompt 单次消费、HTTP fake transport/default disabled、subagent placeholder、redaction。
- 测试：async 顺序/队列满、timeout、shutdown、prompt exact next-request timing、HTTP user/project merge、固定 header allowlist、凭证/代理 header 拒绝、localhost/IP literal/private/link-local/reserved 拒绝、DNS 解析到非公网时零请求、pin/peer 校验、placeholder；响应/错误/headers/status 只进遮蔽截断诊断。
- 完成：before_tool async 非法，无直接 subprocess/真实网络。
- 禁止：Provider/MCP client 或工具实现修改。

## T6：Shell gateway 安全契约

- 前置条件：T5。
- 允许文件：hooks/actions.py、hooks/lifecycle.py、必要时 newcode/permissions/manager.py；tests/test_hooks_actions.py、必要时 tests/test_agent_scheduler.py、tests/test_permissions_manager.py。
- 实现：固定 command 经 Permission→ToolScheduler→executor，origin suppression、输出截断遮蔽。
- 测试：Permission deny/confirmation、sandbox/hard deny、serial scheduler；Permission=require_confirmation 时 sync 与 async shell 均不调用 confirmer/Input、不执行命令、不自动批准，仅产受限安全诊断；普通 Agent 工具原有交互 confirmer 测试继续通过。
- 完成：Hook shell 不能将 deny 或 require_confirmation 转 allow；拒绝时命令执行计数为零。
- 禁止：放宽 run_command schema 或 Permission policy。

## T7：AgentLoop turn/message/model events

- 前置条件：T4–T6。
- 允许文件：newcode/agent/loop.py、newcode/prompt/modules.py、必要时 agent/events.py、tests/test_agent_loop_hooks.py、tests/test_prompt_hooks.py。
- 实现：精确发 turn、message、model、cancel、exception events；user_message_received 只提供先脱敏再截断到 512 字符的 message.summary；实现 injection 事件后下一个主请求的消费时序。
- 测试：event order/context、message.summary、exact/glob/regex、脱敏/512 上限、normal/error/cancel/max iterations；before_model_request 同步 injection 进入当前请求；after_tool/after_model_response injection 进后续请求；async 错过当前请求不得修改已构造 messages；单次消费、session-end discard、持久化零写入。
- 完成：Provider 无 Hook import，Context summary 不触发 Hook。
- 禁止：Context/Memory/Skill/Provider 核心策略修改。

## T8：before_tool deny 和 after_tool

- 前置条件：T7。
- 允许文件：agent/loop.py，必要时 agent/scheduler.py，相关 Hook/Permission/MCP/Skill tests。
- 实现：mode/visible/Permission allow 后同步 before_tool；仅规则级合法 deny 以 hook_tool_denied ToolResult 原序回灌；shell 内部 ToolResult 不进 session；其他 action 结果仅诊断。
- 测试：Plan/Do、Permission、hard deny、sandbox、Skill/MCP whitelist、工具原序和 read-only 调度；shell/HTTP/prompt/subagent 成功、拒绝、timeout、失败均不回灌或进入主 ChatSession。
- 完成：Hook 不绕过任何 gate，也不递归。
- 禁止：新 AI 工具、MCP runtime、Permission semantics 改动。

## T9：AgentLoop 组合回归

- 前置条件：T7–T8。
- 允许文件：测试；失败直接证明时仅最小 hooks 或 AgentLoop 修复。
- 实现：组合 fake provider、tools、Context/Memory/MCP/Skill fixtures。
- 测试：Hook 和既有 AgentLoop、Context、Memory、Permission、MCP、Plan/Do、Skill tests。
- 完成：无 Hook 时行为和工具排序不变。
- 禁止：为便利改 CLI/Provider。

## T10：CLI 加载与 system/session 生命周期

- 前置条件：T2、T7–T9。
- 允许文件：newcode/cli.py、tests/test_cli_hooks.py，必要时 tests/test_cli_session.py。
- 实现：启动加载、system/session events，clear/resume 结束旧 scope 并建新 scope。
- 测试：new/restore/clear、配置诊断、once/pending reset、静态命令回归。
- 完成：session archive 不保存 injection/state。
- 禁止：新增 Hook slash command 或修改 Command Registry。

## T11：CLI finally 与后台 shutdown

- 前置条件：T10。
- 允许文件：cli.py，必要时 Hook lifecycle/actions，CLI Hook/lifecycle tests。
- 实现：checkpoint→Hook shutdown→Memory→Context→MCP，所有退出路径隔离失败。
- 测试：EOF、exit、KeyboardInterrupt、异常、正常退出、shutdown timeout。
- 完成：不残留 worker 或跨 session state。
- 禁止：阻断或重排现有服务内部 cleanup，tmux/真实网络。

## T12：targeted 回归与静态审计

- 前置条件：T1–T11。
- 允许文件：原则仅测试；失败直接证明时最小修复和回归测试。
- 实现：审计 Provider、递归、持久化、路径、shell/HTTP 与执行边界。
- 测试：全部 Hook 和相关 AgentLoop/CLI/Permission/Context/Memory/MCP/Skill/Command tests；包含 regex 灾难回溯/长度/预算、shell confirmation zero-execution、HTTP SSRF/header 和 DNS pinning fake/local tests。
- 完成：compileall、targeted pytest、git diff --check 通过。
- 禁止：范围外特性或真实网络/secret。

## T13：full pytest 与受控 CLI 验收

- 前置条件：T12。
- 允许文件：仅失败直接证明的最小修复。
- 实现：fake provider、临时 home/workspace、fake/local HTTP 流完成 lifecycle。
- 测试：全量 pytest，记录 skip 原因。
- 完成：无真实网络、生产 secret、第三方 MCP、tmux 安装、提交或 push。
- 禁止：扩展本章范围。

## 执行顺序

T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10 → T11 → T12 → T13。

每个 Phase 必跑 python -m compileall newcode 与该 Phase targeted pytest。任一失败、Hook 可能绕过安全 gate、需要真实网络或范围外文件时停止并请求审批。
