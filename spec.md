# Chapter 7：MCP Client —— 让 NewCode 接入外部工具生态

## 背景

NewCode 已具备六个内置工具、Tool Registry、同步 Tool executor、AgentLoop、ToolScheduler、Plan Mode / Do Mode、Prompt System 和 Permission System。当前 Tool Registry 只能登记本地工具，模型不能发现或调用外部 MCP Server 的 tools。

本章新增 MCP Client：NewCode 启动时从配置发现 MCP Server，独立连接并获取 tools；成功发现的远端工具被适配为现有 Tool 后注册到 Tool Registry。模型调用 MCP tool 时，仍通过现有 tool schema、AgentLoop、Permission System、ToolScheduler 和 ToolResult 链路工作，不理解 transport、JSON-RPC 或连接生命周期。

本章统一使用 NewCode，不使用 MewCode。

## 目标

- 支持 stdio 和 Streamable HTTP transport。
- 使用官方 MCP Python SDK 管理协议兼容、transport、tools/list、tools/call 和 shutdown。
- 支持多 server 配置、连接缓存、工具发现、调用和故障隔离。
- 将 MCP name、description、inputSchema、arguments、result、error 映射为现有 Tool 抽象。
- MCP tool 与内置 tool 一样经过 Plan/Do Mode、Permission System、ToolScheduler 和 Tool executor。
- 不允许远端工具覆盖内置工具或其他 server 的工具。
- 一个 server 失败不影响内置工具、其他健康 server 或 CLI 启动。

## 非目标

本章只实现 MCP tools，不实现 MCP resources、resource templates、prompts、sampling、tasks、Apps、MCP Server、legacy SSE、自动重连、health checking、复杂 OAuth、network sandbox、MCP 专属复杂 UI、项目指令、自动记忆、真实 Skill 加载或自动化评估。

本章不因远端 tool 的名字或 description 看起来只读，就给予只读或低风险信任。

## 当前代码约束

- ToolSpec、ToolRegistry 与 DeepSeekProvider 当前不校验 function name 的字符集或长度；Chapter 7 必须在 MCP adapter 注册前主动校验。
- ToolRegistry 以工具名作为唯一键，重复注册会失败；当前只读/副作用分类仅覆盖六个内置工具，to_openai_tools() 按注册顺序导出。
- Tool 是同步 run(arguments, context) 到 ToolResult 协议；Tool executor 已将异常转为结构化失败并仅遮蔽 ToolContext.sensitive_values。
- AgentLoop 在 ToolScheduler 前做 Plan/Do Mode 与 PermissionManager 预检查，并回写工具结果；当前 Do Mode 白名单固定为六个内置工具。
- ToolScheduler 只并发连续只读工具；所有非只读工具按模型原始顺序串行。
- PermissionManager 优先级为 hard denylist、workspace sandbox、session/local/project/user rules、built-in policies、permission mode、HITL；当前 sandbox 只处理本地文件工具，hard denylist 只处理 run_command，rule matcher 只支持 command/path。
- CLI 当前没有 MCP resource lifecycle 或 finally shutdown hook；本章必须新增。
- 当前 pyproject.toml 没有 MCP dependency，AppConfig 没有 MCP server 配置。

## 架构和边界

调用链必须为：

AgentLoop
-> Plan/Do Mode gate
-> PermissionManager
-> ToolScheduler / execute_tool_call
-> MCPToolAdapter.run
-> MCPManager.call_tool_sync
-> MCP SDK async client / transport
-> MCP Server

新增 newcode.mcp 包：

- config.py：配置、用户/项目加载合并、变量展开、配置错误。
- types.py：MCPServerConfig、MCPServerStatus、MCPToolDescriptor。
- naming.py：稳定 namespace 和冲突检测。
- runtime.py：同步 Tool 到异步 SDK 的受控 runtime bridge。
- manager.py：MCPManager，负责 startup、connection/client cache、discovery、call、shutdown。
- adapter.py：MCPToolAdapter，实现既有 Tool 协议。

MCPManager 和 adapter 不负责编排 AgentLoop、Prompt strategy、Provider logic 或 Permission policy。

AgentLoop 保持模型循环、模式检查、权限预检查、调度和结果回写职责；不得实现 JSON-RPC、request id、pending response correlation、transport 或 SDK lifecycle。

Provider 继续只负责 Chat Completions messages/tools 和 ProviderEvent。Provider 不得导入 newcode.mcp、连接 MCP Server、理解 MCP namespace、MCP error、PermissionManager、AgentMode、system-reminder 或 cache_control。

Prompt System 不负责 MCP transport、connection 或 discovery。它可以增加稳定提示：外部工具同样受权限检查；不得注入 URL、headers、env、server status、远端 error 或 secret。

## MCP 协议与 SDK 决策

本章必须使用官方 MCP Python SDK，不自行实现 JSON-RPC 2.0 transport。

当前项目尚未安装 MCP SDK；implementation 开始前必须选定并锁定一个官方 MCP Python SDK 版本，并以该版本公开 API 验证 stdio、Streamable HTTP、协议兼容、tools/list 分页、tools/call 和 shutdown 行为。手写 JSON-RPC 会额外承担 request id、pending correlation、error response、取消与关闭，也会把传统 initialize 到 tools/list 到 tools/call 错误固化为唯一生命周期。

NewCode 不得硬编码必须先调用传统 initialize。若锁定 SDK 的公开兼容模式支持传统 initialize/session server 和现代无传统 initialize/session server，应由 SDK 完成相应协商；若其公开 API 不支持某种 server，应将该 server 标为不可用，不得以手写 fallback 扩大范围。

本章只在 SDK 层覆盖 server discovery/protocol compatibility、tools/list（以锁定 SDK 版本公开分页 API 为准）和 tools/call。不实现 legacy SSE。Streamable HTTP headers 必须使用锁定 SDK 版本公开推荐的 HTTP client/transport 注入方式，不能依赖未验证或已过时的参数。

## 同步 Tool 与异步 SDK

MCPManager 必须拥有受控 async runtime bridge：

- 独占 async runtime、SDK context、HTTP client、stdio subprocess 和 shutdown。
- MCPToolAdapter.run 只能通过 MCPManager.call_tool_sync 发起同步等待调用。
- adapter 不得自行创建 event loop、subprocess 或 HTTP client。
- 同一 server 的 session 调用必须串行化；不得假定 SDK session 或远端 server 支持并发 in-flight request。
- 成功 server 的连接在 NewCode 生命周期内缓存复用。
- 本章不自动 reconnect；失效连接标记 unavailable，直到下次启动。
- CLI 在 EOF、/exit、KeyboardInterrupt、启动后异常等所有退出路径的 finally 中调用 MCPManager.shutdown。

## 配置

### 配置位置与合并

新增两层 MCP config：

- 用户级：~/.newcode/mcp.yaml
- 项目级：<workspace_root>/.newcode/mcp.yaml

顶层为 mcp_servers map，示例：

    mcp_servers:
      local_server:
        transport: stdio
        command: python
        args: [server.py]
        env:
          TOKEN: "${LOCAL_TOKEN}"

      remote_server:
        transport: streamable_http
        url: "https://example.com/mcp"
        headers:
          Authorization: "Bearer ${REMOTE_TOKEN}"

合并顺序为 user 到 project。同名 server 由项目级条目完整覆盖用户级条目，不做字段级 merge，避免 endpoint 与 credential 意外拼接。没有 mcp_servers 代表不使用外部 MCP tools，不是错误。

### 校验和变量展开

- server name 必须为稳定 ASCII 配置键，并满足 namespace 映射限制。
- transport 只能为 stdio 或 streamable_http。
- stdio 的 command 必填；args 是可选字符串列表；env 是可选字符串 map。
- Streamable HTTP 的 url 必填且为 http/https URL；headers 是可选字符串 map。
- 缺字段、错误类型、未知 transport 或 transport 不适用字段均为该 server 的配置错误。
- env 和 headers 的字符串值支持一个或多个 ${VAR}。
- 任一变量缺失或为空时，对应 server 为 mcp_config_error；不得把未展开占位符发给 server。
- 展开后的 secret 只能留在 manager 私有运行时配置，绝不写入 session、prompt、ToolResult metadata、普通日志、CLI 输出或错误文本。
- stdio 子进程继承当前进程环境，再由已验证、已展开的 config env 显式覆盖；MCPManager 必须把展开值纳入私有敏感值集合，避免它们进入结果、异常、诊断或日志。

## Transport

### stdio

使用官方 SDK 的 stdio server parameters/transport 启动子进程，传入 command、args 和已验证 env。SDK context 负责关闭 stdin、等待进程并在需要时终止。server stderr 仅进入受控诊断位置，不得泄露 secret。

### Streamable HTTP

使用官方 SDK 的 Streamable HTTP transport。headers 通过锁定 SDK 版本公开要求的 HTTP client/transport 创建方式传递。MCPManager 拥有并在 shutdown 时关闭 HTTP client。URL credential、headers 和远端错误中的已知 secret 必须遮蔽。

sse、legacy_sse 和其他未声明 transport 一律使该 server 配置错误；不得新增 SSE 分支。

## MCPManager 生命周期和隔离

CLI 启动顺序：

1. 创建内置 ToolRegistry、ToolContext 和 PermissionManager。
2. 加载、合并、校验 MCP config，展开环境变量。
3. 创建 MCPManager。
4. 对每个有效 server 独立连接并执行 tools/list discovery。
5. 仅将成功发现且通过 schema/name 校验的 MCPToolAdapter 注册到 ToolRegistry。
6. 保存 MCPServerStatus；失败只输出不含 secret 的简洁 server warning。
7. 继续启动 CLI，即使所有 MCP server 失败。

CLI 必须将 MCPManager 的整个启动后生命周期置于 finally：无论 EOF、/exit、KeyboardInterrupt、交互循环异常或启动后异常，均调用幂等 shutdown。启动前发生的配置错误仍不得阻止内置工具和 CLI；仅将对应 server 标记 unavailable。

单 server 的配置、启动、协议协商、tools/list、单 tool schema 或命名失败不得回滚内置工具、其他 server 或同 server 的其他合法 tool。单个 schema/name 失败时跳过该 tool，继续注册同 server 的其余合法 tool。失败 server 在本进程标为 unavailable；本章不自动重试。

每个 ready server 有独立 runtime/client/connection cache；同 server 的 tools/list 与 tools/call 复用 cache，不重复启动 subprocess 或创建 HTTP client。不同 server 不共享 session、headers、env、lock 或失败状态。tools/list 必须处理分页。

远端 isError、SDK、transport、协议异常均转换为 ToolResult.failure，不能让 AgentLoop 崩溃。失效连接后调用返回 mcp_server_unavailable 或 mcp_call_failed，不自动 reconnect。

shutdown 必须幂等：关闭每个 SDK context、HTTP client、stdio subprocess 并清空 cache；一个 server 关闭失败不阻止其他 server 关闭。

## MCP Tool Adapter

每个远端 MCP tool 对应一个 MCPToolAdapter，并实现现有同步 Tool 协议。

- server config key 映射为 adapter server identity/metadata。
- remote name 映射为原始 remote tool identity。
- remote description 映射为 ToolSpec.description，并附带安全来源说明。
- inputSchema 映射为 ToolSpec.parameters。
- ToolCall.arguments 以原样 JSON object 传给 tools/call。
- CallToolResult.content 和 structured_content 映射为 JSON-safe ToolResult.data。
- CallToolResult.isError 映射为 ToolResult.failure，code 为 mcp_tool_error。
- SDK/transport/protocol exception 映射为 ToolResult.failure，code 为 mcp_call_failed 或 mcp_server_unavailable。

inputSchema 采用轻量结构校验，不新增 JSON Schema validator dependency：它必须是 JSON object，type 只能为 object 或省略，properties/required/additionalProperties 如存在必须是可转发的 JSON 值；不合格的单个 remote tool 不注册，错误码 mcp_tool_schema_invalid。adapter 保留 content block 的必要 type 和 structured content，不能伪造成功。metadata 只可含 mcp_server、mcp_tool、transport 等安全 identity。MCPManager 必须遮蔽该 server 展开的 secret；既有 Tool executor 继续遮蔽 ToolContext.sensitive_values。MCP failure 不是 ProviderError。

### 调度分类

远端 tool 的真实副作用和风险不能从 description/inputSchema 可靠判断。本章所有 MCPToolAdapter 固定为 Do Mode 可见、非只读、有副作用，必须由 ToolScheduler 串行执行。不得因名称含 read/search 而放入并发只读批次。

ToolRegistry 必须从六个固定名称分类演进为内置默认分类加 adapter 显式分类，但六个内置工具的分类和行为必须不变。

## 工具命名和冲突

MCP tool 名既是 ToolRegistry key，又是 OpenAI-compatible function name，因此不使用带点号的 mcp 点 server 点 tool 格式。

稳定名称格式：

    mcp__{server_slug}__{tool_slug}__{identity_digest}

- server_slug、tool_slug：小写 ASCII 安全展示片段，固定长度截断。
- identity_digest：SHA-256 对 server_config_key、NUL、remote_tool_name 的 UTF-8 拼接取固定长度前缀。
- NewCode 主动校验最终名只使用 ASCII [A-Za-z0-9_-]，且最大 64 字符；不依赖 Provider 进行校验。
- slug 可以为满足 64 字符上限而截断；identity_digest 的固定长度不得截断。
- 原始 server key 与 remote tool name 存在 adapter metadata。
- 算法、截断、最大长度和 digest 长度固定并有单元测试。

示例：

    mcp__github__search_issues__4a1bc29d3e10

相同 server key 与 remote tool name 跨启动生成同名；不同 server 的同名 remote tool 生成不同名；slug 相同但原始 identity 不同由 digest 区分。

若生成名已存在、发生 digest 碰撞、超过长度限制或与内置工具冲突，拒绝注册该 MCP tool，记录 mcp_name_collision；绝不覆盖已有工具。ToolRegistry.register 的重复保护保留为最后防线，MCPManager 必须先主动检测。

## Plan Mode 和 Do Mode

- Plan Mode 继续只暴露 read_file、find_files、search_code。
- Plan Mode 不暴露 MCP tool；即使模型伪造 namespaced MCP call，也按 disallowed_tool 拒绝，且不得执行权限确认或远端调用。
- Do Mode 暴露六个内置工具和成功注册、标记为 Do-visible 的 MCP tools；这是对当前固定六工具 Do Mode 白名单的新增能力。
- ToolRegistry 必须提供 mode-aware schema export 或等价可见性接口；AgentLoop 必须使用它，不能继续只使用固定 DO_TOOL_NAMES。
- Plan Mode 限制优先于 PermissionMode、显式 allow rule、HITL 和 MCP Server。

## Permission System 接入

必经调用链：

AgentLoop
-> Plan/Do Mode gate
-> PermissionManager.check
-> ToolScheduler
-> execute_tool_call
-> MCPToolAdapter
-> MCPManager / MCP Server

adapter、CLI、Provider 和 PromptBuilder 不得绕过该链路直接调用远端 server。

Permission normalizer 必须为 MCP adapter 产生 normalized_args：

- mcp_server：原始 server config key。
- mcp_tool：原始 remote tool name。
- mcp_transport：stdio 或 streamable_http。
- mcp_arguments：当次 JSON arguments，仅用于决策和调用，不进入 prompt 或普通日志。

MCP 调用和内置工具一样先进入 PermissionManager；但当前 hard denylist 仅匹配 run_command、workspace sandbox 仅匹配本地文件工具，无法自动约束任意远端 MCP 参数或远端 server 自身环境。Chapter 7 不得声称它们已 sandbox 远端 server；必须新增 MCP 专属保守 built-in policy。

PermissionMatch、YAML parser、rule matcher、SessionPermissionRules 和 CLI confirmation summary 必须扩展 mcp_server、mcp_server_glob、mcp_tool、mcp_tool_glob。规则仍以 namespaced ToolSpec.name 作为 tool，可再以 MCP identity 缩小范围：

    rules:
      - id: allow_github_issue_search
        tool: "mcp__github__search_issues__4a1bc29d3e10"
        match:
          mcp_server: "github"
          mcp_tool: "search_issues"
        action: allow
        reason: "Allow approved GitHub issue search"
        risk_level: medium

### 保守默认

- 所有 MCP tool 先经过统一 PermissionManager；hard denylist 与 workspace sandbox 继续在其现有适用范围保护内置操作，session/local/project/user explicit rules 可通过新增 MCP identity match 规则匹配远端工具。
- 远端行为无法可靠判定，因此新增 built-in external MCP policy：未命中 explicit allow/deny 的 MCP tool 一律返回 require_confirmation，risk_level 为 high。
- 此 built-in policy 位于 explicit rules 之后、permission mode 之前；所有 permission mode，包括 permissive 和 trusted，均不得自动放行未知 MCP tool。只有明确 allow rule 或用户确认可以放行。
- 用户选择 session confirmation 时，SessionPermissionRules 必须按 mcp_server 与 mcp_tool 创建 session allow rule；once confirmation 不持久化。
- hard denylist、workspace sandbox、explicit deny 和 Plan Mode 在各自适用范围内不可被 MCP、permission mode、allow rule 或 HITL 绕过。
- NewCode 不自动向 MCP Server 传递 workspace root，也不因 adapter 有 ToolContext 就授予远端 server 本地 filesystem 权限。
- 外部 server 的自身环境不是 NewCode workspace sandbox 可技术控制范围；本章不从任意 inputSchema 猜测路径字段并声称远端已被 sandbox。

权限拒绝时，AgentLoop 必须写回既有 permission_denied ToolResult observation，按原始 tool call 顺序写入 session 并回灌下一轮模型；拒绝不得触发远端 transport 或 tools/call。

## 错误模型

- mcp_config_error：配置、transport 或变量展开失败。
- mcp_discovery_failed：连接、协议协商或 tools/list 失败。
- mcp_tool_schema_invalid：单个 remote tool schema 无效。
- mcp_name_collision：稳定名称冲突。
- mcp_server_unavailable：server 未 ready、已关闭或连接失效。
- mcp_call_failed：SDK、transport 或协议异常。
- mcp_tool_error：远端 CallToolResult.isError 为 true。

所有错误转为 ToolResult.failure，包含安全 server/tool identity 和稳定 code。不得含 headers、env、URL credential、token、完整 stack trace 或未遮蔽 secret。MCP failure 不能成为 ProviderError，也不能终止整个 AgentLoop。

## 兼容边界

- CLI 创建内置 registry 后创建 MCPManager、执行 discovery、注册成功 adapter，并在 finally 中 shutdown。
- 单 server failure 只输出安全摘要，不阻止 CLI。
- MCP config、connection status、headers、env、discovery payload、secret 不进入 session 或 prompt。
- /plan、/do、/exit 的既有行为不变且不污染 session。
- Prompt 可提醒外部工具受权限检查；真正安全边界仅在 Permission System。
- Provider 不导入 MCP、PermissionManager、PromptBuilder、system-reminder 或 cache_control。
- 不新增 cache_control 或任何 Provider 专属缓存策略。
- 六个内置工具名称、schema、执行逻辑、workspace 限制和 run_command 安全能力保持不变。

## 功能需求

- F1：支持 stdio 和 Streamable HTTP，不支持 legacy SSE。
- F2：使用锁定版本官方 MCP Python SDK 的公开兼容机制；NewCode 不手写 JSON-RPC。
- F3：支持 user 到 project config merge；同名项目 server 完整覆盖用户 server。
- F4：env/header 变量展开失败只使对应 server unavailable。
- F5：MCPManager 缓存并隔离每个 server connection/client，退出释放资源。
- F6：config、startup、discovery、schema、call failure 必须 server-isolated。
- F7：adapter 映射 name、description、inputSchema、arguments、result、error。
- F8：namespaced name 稳定、可测试，不静默覆盖。
- F9：MCP tool 仅 Do Mode 可见；Do Mode 外部工具可见性是 Registry/AgentLoop 的新增能力，Plan Mode 永不执行。
- F10：全部 MCP tool 串行调度。
- F11：全部 MCP tool 先经过 PermissionManager；拒绝不得调用远端。
- F12：未命中 explicit allow/deny 的 MCP tool 默认 require_confirmation；所有 permission mode 包括 trusted 都不能静默放行。
- F12a：session confirmation 必须为 MCP server/tool identity 建立可匹配的 session allow rule。
- F13：permission_denied 作为 tool observation 回灌模型。
- F14：Provider、Prompt、AgentLoop 与内置六工具职责边界不退化。

## 验收标准

- AC1：stdio config 正确解析 command、args、env，并能由 SDK 管理启动/关闭。
- AC2：HTTP config 正确解析 url、headers，并用锁定 SDK 版本公开推荐的方式传递 headers。
- AC3：user/project config merge 正确；同名项目 server 完整覆盖用户 server。
- AC4：变量可展开；缺失变量不发送未展开占位符且不泄露 secret。
- AC5：多 server 独立 discovery；一个 server failure 不影响内置工具和其他健康 server。
- AC6：tools/list 分页完整收集。
- AC7：adapter schema、arguments、success mapping 正确。
- AC8：isError 和 SDK/transport exception 都变为稳定 ToolResult failure。
- AC9：不同 server 同名 tool、slug 冲突、内置工具冲突均不会静默覆盖。
- AC10：稳定名称跨启动一致，名称只含 ASCII [A-Za-z0-9_-] 且最大 64 字符；仅 slug 截断，digest 不截断。
- AC11：同 server 复用 connection/client cache；CLI finally 在 EOF、/exit、KeyboardInterrupt 和启动后异常时调用幂等 shutdown，清理 HTTP/stdio/runtime resource。
- AC12：Plan Mode 不暴露且不执行 MCP tool；Do Mode 暴露成功注册 MCP tool。
- AC13：MCP tool 不进入 read-only 并发批，调用和回写顺序稳定。
- AC14：PermissionManager 在 MCP call 前生效；hard denylist、workspace sandbox、explicit deny、Plan Mode 不被削弱。
- AC15：未命中 explicit allow/deny 的 MCP tool 在 strict/default/permissive/trusted 均不会静默放行，需要 explicit allow 或确认；session confirmation 可建立 MCP identity session rule。
- AC16：permission_denied observation 回灌下一轮模型，且拒绝不调用远端。
- AC17：CLI 管理 startup/shutdown，server failure 不阻止 CLI。
- AC18：Provider 保持 MCP-independent。
- AC19：六个内置工具名称、schema、分类、基本行为不变。
- AC20：MCP 新测试和既有完整 pytest 一起通过。

## 测试范围

至少覆盖：stdio config parsing、HTTP config parsing、user/project config merge、环境变量展开、missing variable、multiple MCP servers、one server failure isolation、tools/list discovery 与分页、adapter schema mapping、tools/call success、tools/call error、duplicate remote names、内置工具冲突、connection/client caching、shutdown cleanup、Permission System cannot be bypassed、AgentLoop can invoke MCP-adapted tool、Provider remains MCP-independent、六个工具不变和全量 pytest 回归。

建议新增：

- tests/test_mcp_config.py
- tests/test_mcp_manager.py
- tests/test_mcp_adapter.py
- tests/test_mcp_permissions.py
- tests/test_agent_loop_mcp.py
- tests/test_cli_mcp.py

同时扩展 Provider、ToolRegistry、AgentLoop、CLI、Permission 与既有六工具回归测试。

## 预计模块变更

预计新增：

- newcode/mcp/__init__.py
- newcode/mcp/config.py
- newcode/mcp/types.py
- newcode/mcp/naming.py
- newcode/mcp/runtime.py
- newcode/mcp/manager.py
- newcode/mcp/adapter.py

预计修改：

- pyproject.toml：添加并锁定官方 MCP Python SDK dependency；implementation 开始前依据该版本公开 API 确认 transport、compatibility 与分页调用。
- newcode/tools/types.py、newcode/tools/registry.py、newcode/agent/mode.py：支持 adapter 显式分类和 Do Mode 外部工具可见性的 dynamic schema export。
- newcode/agent/loop.py：仅接入 Registry 的 mode-aware export，不加入 MCP protocol。
- newcode/permissions/normalizer.py、rules.py、modes.py、types.py、session.py、confirmer.py：增加 MCP identity、rule match、session allow 与 conservative built-in policy。
- newcode/cli.py：MCPManager startup/discovery/shutdown，且以 finally 覆盖所有交互退出路径。
- newcode/prompt/modules.py：仅可增加稳定的外部工具权限提醒。

不得修改 Provider 职责，不得把 MCP transport 放进 PromptBuilder，不得改变六个内置工具的执行实现。
