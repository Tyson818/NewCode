# Chapter 12：Hook 系统——实施计划

## 架构

Hook 由五层构成：独立 YAML loader 读取 user/project 规则；纯数据模型和条件 matcher 做集中校验；HookEngine 管理有效顺序、once 和 transient injection；HookActionRunner 管理 action 与后台 worker；CLI/AgentLoop 仅在精确生命周期点发射事件。

数据流为 user/project hooks.yaml 到 HookLoader，再到 HookRuleSet 和安全 diagnostics；HookEngine emit(event, HookContext) 经 matcher 得到稳定顺序 action outcome；结果进入 transient prompt queue、受控 tool gateway、restricted HTTP 或 subagent placeholder。Provider、MCP manager 和工具实现不感知 Hook。

## 模块设计

| 模块 | 职责 | 依赖边界 |
|---|---|---|
| newcode/hooks/types.py | Rule、Action、Condition、Context、Outcome、Diagnostic | 纯数据，无执行层 import |
| loader.py | 两个 YAML 根、路径检查、单条隔离和规则/用户网络授权合并 | yaml、Path、types |
| conditions.py | exact/glob/regex/not、all/any | 纯 context matcher |
| engine.py | emit、once、稳定顺序、递归抑制、before_tool 规则级 deny 决策 | types、conditions、action protocol |
| actions.py | 单 worker/队列、timeout、HTTP、placeholder、redaction | 注入 gateway/transport，不直接碰 Provider/MCP |
| lifecycle.py | AgentLoop/CLI 唯一 facade，构造受限 context | engine/actions |
| agent/loop.py | turn/model/tool 事件、Permission 后 deny 回灌、消费 injection | 不改变 Provider contract |
| prompt/modules.py | request-only hook_injections 槽位 | 不写 session |
| cli.py | load、system/session 事件、clear/resume、finally shutdown | 保持现有 cleanup 语义 |

必要时仅最小修改 agent/events.py 公开安全结束状态。不得改 Provider、Permission rule parser、MCP runtime、ToolRegistry 或 Command Dispatcher。

## 关键设计

### Loader 与合并

默认路径为 Path.home()/.newcode/hooks.yaml 与 workspace/.newcode/hooks.yaml。project 规则覆盖 user 同 ID；输出顺序为所有未覆盖 user 后接 project。来源内重复只隔离后项。Loader 返回安全码和来源，不回传 YAML 内容。只有 user 文件可含 network.enabled 与 network.allow_hosts；project network 字段非法。project HTTP action host 必须与 user allow_hosts 精确匹配。配置合并测试必须证明 project 不能启用 HTTP，也不能增加或扩大 host allowlist。

### 条件

条件只能读取 allowlisted HookContext。exact/glob 使用 Permission 的 normalizer 和 fnmatchcase 思路；regex/not 在 Hook matcher 独立实现，不能改变 Permission semantics。all/any 只允许一个层级。regex pattern 上限为 256 字符，字段值上限为 1024 字符（message.summary 仍最多 512）。不得用无界 Python re 匹配不可信配置；将语法编译到有线性时间上界的 matcher。安全子集仅含字面字符、转义字面字符、字符类、点、^/$、? 和上限 256 的有界 {m,n}；拒绝分组、反向引用、lookaround、交替、*、+ 与嵌套/歧义量词。超限/越界输入使规则无效或不匹配并产生安全诊断，不能阻断 AgentLoop。

### Action gateway

AgentLoop 提供窄 HookToolGateway，shell action 以固定 run_command ToolCall 经过 Permission、ToolScheduler、executor；gateway 的 hook-origin 标记防止 before_tool 和 after_tool 重入。Hook gateway 使用 PermissionManager 的非交互检查路径：沿用当前规则、模式、hard deny 与 sandbox 检查，将 require_confirmation 作为 deny-by-default，不触发 CLI/HITL confirmer。sync/async shell 均在确认要求时零执行并仅记安全诊断；普通 Agent 工具确认行为保持原状。HTTP 不共享 MCP client，默认 disabled，只能经 user host allowlist 和 fake/local transport 测试。HTTP header 名固定为 Accept、Content-Type、User-Agent，拒绝凭证/代理相关头。host 精确授权后仍校验解析出的所有地址为公网，并 pin 实际连接至通过校验的地址、验证 peer；DNS 解析到任意非公网地址、localhost 或不能 pin/验证均在发请求前拒绝。后台采用单 worker、有界队列。

### Prompt 时序与生命周期

before_model_request 在最终主请求 messages 构造前同步发射，使此 event 的同步 injection 可进入当前请求；其他同步事件（如 after_tool、after_model_response）的 injection 只进入该 event 之后的下一次主请求。异步 injection 若错过当前构造/发送，只能进入后续请求或 session end 丢弃，不能改已构造/已发送 messages。每条 injection 单次消费；injection 不进 session、JSONL、Memory、artifact、log、CLI。

once scope 固定映射：system→CLI process ID；session→ChatSession ID；turn→session ID + turn index；message→session ID + message ID；tool→session ID + tool call ID。clear、新 session、resume 开新 session scope；once 不持久化、不跨进程。

before_tool 仅在 mode、Skill visible set 和 Permission allow 后同步运行；仅该 event 的规则级 deny（不是 action type）会按 call 原序回灌既有 ToolResult，且不会运行同条 action。其他 action 的结果（包含 shell gateway 内部 ToolResult、HTTP response/error、placeholder）只进 Hook diagnostics/log，不进入模型或主 session。shell 内部 ToolResult 供 Hook runner 判断后丢弃。

### HookContext

user_message_received 可选提供固定字段 message.summary，最多 512 Unicode 字符，按既有敏感值/敏感键规则遮蔽后截断；exact/glob/regex 都作用于该脱敏摘要。context 不提供完整用户消息、完整 prompt、完整工具结果或原始异常。

### Action result boundary

只有 before_tool 合法 deny 生成模型可见的 hook_tool_denied observation。shell/HTTP/prompt/subagent 结果都只能成为受限脱敏诊断；shell gateway 内部 ToolResult 仅供 Hook runner 判断，绝不加入主 ChatSession 或回灌模型。

## 文件组织

    newcode/hooks/__init__.py
    newcode/hooks/types.py
    newcode/hooks/loader.py
    newcode/hooks/conditions.py
    newcode/hooks/engine.py
    newcode/hooks/actions.py
    newcode/hooks/lifecycle.py
    tests/test_hooks_types.py
    tests/test_hooks_loader.py
    tests/test_hooks_conditions.py
    tests/test_hooks_engine.py
    tests/test_hooks_actions.py
    tests/test_agent_loop_hooks.py
    tests/test_prompt_hooks.py
    tests/test_cli_hooks.py

## Phase 划分

### Phase 1：规则基础与发现，T1–T3

建立 types、loader、路径 sandbox、覆盖合并和 matcher。依赖无；风险是路径逃逸、规则隔离和误改 Permission。退出门是 compileall 及 types/loader/conditions targeted pytest 通过，且没有 action 执行。

### Phase 2：引擎、action 和可靠性，T4–T6

建立顺序 emit、once、递归抑制、prompt queue、同步 deny、单 worker、shell gateway、HTTP 保守默认和 placeholder。依赖 Phase 1；风险是异步 deny、直接 shell、真实网络、泄密。退出门是 engine/actions tests 证明异常隔离、timeout/shutdown、脱敏和零旁路。

### Phase 3：AgentLoop 集成，T7–T9

加入 turn/message/model/tool 事件、permission-first before_tool、after_tool 和 transient prompt。依赖 Phase 2；风险是 Plan/Do、Skill、MCP、Permission、scheduler 被绕开。退出门是 AgentLoop/Permission/MCP/Skill/Context/Memory 回归通过且原工具顺序不变。

### Phase 4：CLI 生命周期，T10–T11

加载规则、发 system/session events、支持 clear/resume scope reset，并在 finally 有界 shutdown。依赖 Phase 3；风险是 cleanup 阻断和状态泄漏。退出门是 EOF、exit、KeyboardInterrupt、异常、正常退出均有测试证据。

### Phase 5：最终验收，T12–T13

完整 targeted/full pytest、静态边界审计、fake provider/temp workspace/fake HTTP CLI 验收。依赖 Phase 4；仅失败直接证明时最小修复。无真实网络、生产 secret、第三方 MCP 或 tmux 安装。

## 不得跨越的边界

Hook 不直接执行 Provider、MCP、Permission 或工具；所有工具类 action 回既有安全链。不得扩展到 SubAgent、Worktree、Teams、动态命令、下载、OAuth、RAG 或持久化 injection/once。每个 Phase 先 compileall 与 targeted pytest，失败或需范围外文件立即停止。
