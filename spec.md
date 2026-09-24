# Chapter 12：Hook 系统——生命周期钩子与自动化

## 目标

NewCode 新增声明式 Hook 系统：在受控生命周期节点按 event、可选 if 和 action 执行固定自动化。它服务用户和项目自动化，但绝不成为 Provider、Permission、Plan/Do、workspace sandbox、ToolScheduler、MCP、Context、Memory、Skill 或 Command 的旁路。

## 配置、规则与加载

配置入口固定为用户级 ~/.newcode/hooks.yaml 与项目级 <workspace>/.newcode/hooks.yaml，均是顶层 hooks: YAML 列表；沿用现有独立 Permission YAML loader 形态，不向当前 AppConfig 假定未有字段。缺失文件代表空规则。项目配置必须由已解析 workspace 根推导，拒绝符号链接、父目录逃逸、根外路径和非普通文件。

每项规则为 YAML object，包含稳定唯一 id、必填 event、必填 action，以及可选 if、once、async。仅 before_tool 还可有规则级 deny（含有限静态 reason）；deny 不是第五种 action，命中时直接拒绝且不执行该条 action。示例字段为 id: format_after_write；event: after_tool；if: all 包含 field tool.name 和 exact write_file；action type shell、command git status --short、timeout_seconds 10。

- ID 必须匹配 [a-z0-9][a-z0-9_-]{0,63}，不自动生成，用于覆盖、once、诊断和测试。
- action type 只允许 shell、prompt_injection、http_request、subagent；deny 只允许 before_tool，其他 event 使用 deny 即为非法规则。
- 不合法字段、事件、action、条件、ID、timeout 或单条 YAML 仅产生安全诊断并跳过该条，绝不阻断其他规则或 CLI。
- 有效顺序：先按用户文件声明顺序取未被项目同 ID 覆盖的规则，再按项目文件声明顺序取项目规则。项目同 ID 覆盖用户；同源重复 ID 首条保留、后续隔离为 hook_rule_invalid。无 priority，无模型、命令或运行时动态注册。
- 配置仅 CLI 启动时读取；本章不做热更新。

## 事件与安全 context

Hook context 只能含 allowlisted、JSON-safe、已脱敏且受长度限制的快照；不得含 API key、env、headers、URL credential、完整 prompt、完整工具输出、远端响应或 stack trace。唯一允许匹配的用户文本字段是 user_message_received 中可选的 message.summary：最多 512 个 Unicode 字符，先用既有 sensitive-value/敏感键规则遮蔽，再截断；exact、glob、regex 都仅作用于该脱敏截断摘要字符串。其他条件字段为 event、mode、session.id、turn.index、message.id、tool.name、tool.call_id、tool.read_only、安全 normalized args 摘要、tool.result.ok、tool.error_code、stop.reason 和安全 exception.kind。完整用户消息、完整 prompt、完整工具结果和原始异常永远不在条件 context 中。

| 事件 | 精确位置 | 可拦截 | 内容 |
|---|---|---:|---|
| system_start / system_end | CLI 服务启动完成后 / 顶层 finally、服务 shutdown 前 | 否 | workspace、服务、结束安全摘要 |
| session_start / session_end | ChatSession 与 Context 就绪后 / checkpoint 前；clear/resume 的旧 session 也 end | 否 | session、结束原因 |
| turn_start / turn_end | AgentLoop 前 / final answer 或 stopped 已决定后 | 否 | mode、轮次、stop 摘要 |
| user_message_received | user message 已加入 ChatSession 后 | 否 | 可选 message.summary，最多 512 字符，遮蔽后截断 |
| before_model_request / after_model_response | Context prepare 后、stream 前 / collector 完成、工具决策前 | 否 | iteration、请求或响应摘要 |
| before_tool / after_tool | mode、Skill whitelist、Permission 已 allow 后、scheduler/executor 前 / ToolResult 写入会话并按原顺序发出后 | 仅前者 | ToolCall 或结果摘要 |
| turn_cancelled / turn_exception | 取消与非取消异常路径 | 否 | 取消/异常类别 |

Hook action、后台任务、日志和 deny 回灌都带 hook-origin 抑制标记，不再次触发 Hook。

## 条件

条件只匹配安全 context，不执行 Python、表达式字符串、模板或文件读取。叶子为 field 加恰好一个 exact、glob 或 regex，可选 not true；组合为恰好一个 all 或 any，组合内仅有叶子，禁止混用或递归嵌套。缺字段或条件不匹配只是 skip。

exact/glob 复用 Permission 的规范化和 fnmatchcase 思路。现有 Permission 仅支持 exact/glob；regex/反向是独立 Hook matcher 新能力，绝不改变 Permission rule 解析或决策。regex 编译失败使单条规则无效且只给安全码。

regex pattern 最长 256 Unicode 字符；被匹配的字符串字段最长 1024 Unicode 字符，message.summary 仍受 512 字符上限约束。不得对不可信项目配置使用无界 Python re。实现必须将受限 regex 子集编译为有线性时间上界的 matcher；子集仅允许字面字符、转义字面字符、字符类、点、^/$ 锚点、? 量词和上限不超过 256 的有界 {m,n} 量词，拒绝分组、反向引用、前后查找、交替、*、+ 和嵌套/歧义量词。超长 pattern/字段、子集之外语法或执行预算超限使对应规则无效或本次不匹配，只记录安全诊断，不能阻断 AgentLoop。

## Actions 和安全边界

### before_tool deny

仅同步 before_tool 的规则级 deny 可拒绝；该事件配置 async true 即非法。首个命中的合法 deny 停止剩余 before_tool rules，且不运行该条 action，生成 hook_tool_denied 的既有 ToolResult failure/observation，以原 call ID 和原始排序回灌模型。

mode 不可见、Skill whitelist、hard denylist、workspace sandbox、explicit Permission deny 与 confirmation deny 都在 Hook 前发生且优先。Hook 只能拒绝已允许调用，不能修改参数、不能 allow、不能复活被拒绝调用。Hook timeout、异常、skip、subagent placeholder 不属于 deny。

### shell

shell 只接受 YAML 固定非空 command 与受限 timeout；禁止任何变量、模板、format 或用户消息插值，检测到插值标记即加载失败。它构造 run_command ToolCall，经现有 PermissionManager、ToolScheduler、executor，在 workspace 下执行；hard deny、sandbox、confirmation、timeout 和遮蔽仍生效。Hook 使用 PermissionManager 的非交互检查路径，保留当前规则、模式、hard deny 和 sandbox 判断，但对 require_confirmation 采用 deny-by-default，不调用 CLI/HITL confirmer。若 Permission 结果是 require_confirmation，Hook 不等待输入，也不将其视为 allow；该命令零执行，action 安全失败并仅记录受限诊断。同步与 async shell 都遵守此规则，任何路径不得绕过或自动批准 Permission；普通 Agent 工具仍使用既有交互确认语义。Hook 对 stdout、stderr、详情和日志再截断脱敏，且绝不改 MCP 或工具参数。

### prompt injection 时序

每个 injection 只消费一次，且只进入其产生 Hook event 之后的下一次主模型请求。before_model_request 必须在最终主请求 messages 构造之前同步发射，因此该事件的同步 injection 可进入当前即将发送的请求。其他事件（包括 after_tool、after_model_response）产生的 injection 只能进入后续主请求。异步 Hook 若在当前请求构造/发送之后完成，不能修改已构造或已发送的请求；该 injection 仅可排队给下一次请求，或在 session 结束时丢弃。没有后续主请求时丢弃。

action 仅接收有限静态文本，进入目标请求的动态背景，位于 active Skill 后、项目指令前，并标识 Hook 背景、非授权、需工具核验。请求后或无后续请求时丢弃；绝不写入 ChatSession、JSONL、Memory、Context artifact、日志或 CLI 原文。

### 非拦截 action 结果

只有 before_tool 的合法规则级 deny 可以形成模型可见的 hook_tool_denied ToolResult observation。shell、HTTP、prompt injection、subagent placeholder 的成功、拒绝、超时或失败结果均不得写入主 ChatSession 或回灌模型，只能形成受限、脱敏 Hook diagnostics/log。shell gateway 经既有 Permission→ToolScheduler→executor 得到的内部 ToolResult 仅供 Hook runner 判断和产生日志摘要，不得加入主 Agent 工具历史。

### HTTP 和 subagent

HTTP 默认禁用。只有用户级 ~/.newcode/hooks.yaml 中的 network.enabled true 可启用 HTTP，且只有用户级 network.allow_hosts 可定义 host allowlist；项目配置不能启用 HTTP、不能定义或新增/扩大 allowlist。用户/project 配置合并时，project network 字段一律非法；project HTTP action 的 HTTPS host 必须与用户 allowlist 项精确匹配。仅 GET/POST、有限 timeout/请求/响应字节；禁止重定向、cookie/代理继承、复杂 OAuth 与 URL credential。可用 header 名称固定为 Accept、Content-Type、User-Agent；禁止 Authorization、Cookie、Proxy-Authorization 及其他凭证、身份验证、代理或 hop-by-hop header。精确匹配 allowlist 后，还必须拒绝 localhost、未指定、loopback、link-local、private、multicast、保留及其他非公网 IP。DNS 必须在请求时解析并验证全部候选地址，连接必须 pin 到已验证的公网地址并验证实际 peer，不能只检查 URL hostname 后让 HTTP client 自行解析或重新解析。任一解析结果非公网、解析失败或无法 pin/验证 peer 时不得发出请求。响应、错误、headers、URL credential 和状态只可进入脱敏、截断 diagnostics/log，绝不进入模型、主 ChatSession 或任何持久化内容。测试只能 fake transport 或 local fixture。

subagent action 在本章只返回 hook_subagent_not_available，零创建、零调度、零模拟 SubAgent、Worktree 或 Agent Teams。

## 可靠性与会话

- once key 为 (rule_id, event, scope instance)，不持久化、不跨 CLI 进程恢复；scope 映射固定为：system 事件→当前 CLI process ID；session 事件→ChatSession ID；turn 事件→ChatSession ID + turn index；message 事件→ChatSession ID + message ID；tool 事件→ChatSession ID + tool call ID。clear、新会话、resume 都建立新 session scope，并清空 pending injection；旧 session 的结束事件仍在旧 scope 内完成。
- async true 进入单 worker、有界、稳定顺序队列；每个 action 最多一次且有 timeout。before_tool 永远同步串行。
- EOF、exit、KeyboardInterrupt、异常和正常退出停止接收后台任务、有限等待再安全放弃。cleanup 顺序是 checkpoint 后、Memory/Context/MCP cleanup 前；Hook cleanup 失败不阻断后续。
- action 失败、timeout、队列满或内部异常只产生安全诊断，不中断主流程；合法 deny 例外。

## 错误、边界与非目标

外部仅暴露 hook_config_invalid、hook_rule_invalid、hook_event_invalid、hook_condition_invalid、hook_action_invalid、hook_timeout、hook_action_failed、hook_tool_denied、hook_http_disabled、hook_http_denied、hook_subagent_not_available、hook_shutdown_timeout。日志最多含 rule ID、event、action 类型和受限统计；不得泄露规则原文、命令全文、用户全文、secret、env、headers、URL credential、完整工具结果/远端响应/stack trace。

Provider 不理解 Hook；Hook 不替代 Command、Skill、MCP、Permission、Context、Memory。六个内置工具、MCP、Skill whitelist、静态命令与 session JSONL 格式保持兼容。不做 SubAgent、Worktree、Agent Teams、任意 Python/模板执行、动态命令、市场/下载、复杂 OAuth、网络 sandbox、RAG、向量库、团队同步或 Hook 执行历史持久化。

## 验收标准与测试范围

必须覆盖 YAML 缺字段、非法 event/action、单条隔离；user/project 合并与顺序；exact/glob/regex/not/all/any；regex 256 字符 pattern/1024 字符字段限制、安全子集、线性时间预算、灾难性回溯样例拒绝/有界完成且不阻断 AgentLoop；message.summary 字段脱敏、512 字符边界及三种 matcher；每个事件安全 context；before_tool deny 回灌和所有既有 gate 优先；system/session/turn/message/tool 五种 once scope、scope reset 和不跨进程；before_model 同步 injection 进入当前请求、其他同步事件进入后续请求、async 错过当前请求、单次消费和 session 结束丢弃；非 deny action 结果不得进模型/主历史；shell/HTTP 的 Permission、内部 ToolResult 隔离、确认时零执行、超时、遮蔽与 fake fixture；HTTP 固定 header allowlist、凭证/代理 header 拒绝、loopback/private/link-local/reserved IP literal 与 DNS 非公网解析拒绝且无请求发出、连接 pin/peer 验证；用户级网络授权、精确 host 匹配、project 无法启用或扩大；subagent 零创建；CLI clear/resume/退出；Context、Memory、MCP、Skill、Command、Plan/Do、Permission、scheduler 与完整 pytest 回归。
