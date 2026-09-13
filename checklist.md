# Chapter 7：MCP Client 执行 Checklist

## 全局约束

- [ ] 本清单仅用于逐项实施验收，不构成超范围功能授权。
- [ ] 不手写 JSON-RPC，不实现 legacy SSE、resources、prompts、sampling、tasks、Apps、自动重连、复杂 OAuth、network sandbox 或 MCP 专属 UI。
- [ ] Provider 不依赖 MCP/Permission/Prompt；MCP 不依赖 Provider；AgentLoop 不直接使用 SDK transport。
- [ ] 不新增内置工具，不改变六个现有工具的名称、schema、分类、基本行为或 run_command 安全边界。
- [ ] MCP tool 固定为 Do Mode 可见、有副作用、串行调度；Plan Mode 不可见。
- [ ] 未命中 explicit allow/deny 的 MCP tool 在所有 PermissionMode 下 require_confirmation。
- [ ] config、headers、env、secret、连接细节和远端错误不进入 session、prompt、ToolResult metadata 或普通日志。
- [ ] 每项任务完成均执行 compileall 和所列 pytest；basetemp 始终位于 ${env:TEMP}。

## Phase 1：进入与退出门

进入条件：
- [ ] spec.md、plan.md、task.md 已批准；当前 SDK 尚未验证。

退出门：
- [ ] T1-T2 均通过；锁定 SDK 的公开 API 已满足需求。SDK 不兼容则停止，禁止进入 Phase 2。

本 Phase 禁止事项：
- [ ] 不得创建 runtime/manager/adapter；不得使用私有 API、手写 JSON-RPC 或 legacy SSE。

### Phase 1 SDK 不兼容停止条件

- [ ] 若任一必需公开 API 缺失或行为不符合 spec，立即停止。
- [ ] 只允许更换/锁定 SDK 版本或修订 spec；禁止私有 API、手写 fallback、legacy SSE。
- [ ] 兼容测试恢复通过前，禁止开始 Phase 2-8。

### T1：锁定官方 MCP SDK

- [ ] 前置依赖：无；仅 Phase 1 已进入。
- [ ] 文件变更核对：pyproject.toml
- [ ] 实现核对点：选定并锁定官方 MCP Python SDK 版本；不引入 JSON Schema validator；记录待验证公开 API。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_sdk_compatibility.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase1"
- [ ] 完成证据：依赖固定、SDK 可导入；未改 runtime/Provider/AgentLoop/Registry/Permission/CLI。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T2：验证 SDK 公开 API

- [ ] 前置依赖：T1。
- [ ] 文件变更核对：tests/test_mcp_sdk_compatibility.py
- [ ] 实现核对点：验证 stdio、HTTP headers、list 分页、call success/isError、cleanup、兼容模式；只用公开 API 与 fake/fixture。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_sdk_compatibility.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase1"
- [ ] 完成证据：所有兼容测试通过；不通过时停止，仅可更换版本或修订 spec。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

## Phase 2：进入与退出门

进入条件：
- [ ] Phase 1 退出门已满足。

退出门：
- [ ] T3-T6 通过；配置、命名、schema 均可纯单元测试。

本 Phase 禁止事项：
- [ ] 不得连接 server、修改 AppConfig 既有行为，或接入 CLI/AgentLoop/Permission。

### T3：定义 MCP 核心类型

- [ ] 前置依赖：Phase 1。
- [ ] 文件变更核对：newcode/mcp/__init__.py；newcode/mcp/types.py；tests/test_mcp_config.py
- [ ] 实现核对点：定义 server config/status、tool descriptor、按 server 错误与私有敏感值表示。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_config.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase2"
- [ ] 完成证据：类型/安全 repr 测试通过；模块不导入 SDK runtime、Provider、AgentLoop、Permission、CLI。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T4：加载并合并 MCP 配置

- [ ] 前置依赖：T3。
- [ ] 文件变更核对：newcode/mcp/config.py；tests/test_mcp_config.py
- [ ] 实现核对点：加载 user/project mcp.yaml；完整覆盖同名 server；校验 stdio/HTTP 字段。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_config.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase2"
- [ ] 完成证据：无文件、覆盖、多 server、非法字段通过；不改 AppConfig/CLI。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T5：展开变量与构造 stdio 环境

- [ ] 前置依赖：T4。
- [ ] 文件变更核对：newcode/mcp/config.py；tests/test_mcp_config.py
- [ ] 实现核对点：展开 ${VAR}；缺失/空变量产生 server error；进程环境加显式 env 覆盖；敏感值私有化。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_config.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase2"
- [ ] 完成证据：env/header、多变量、缺失值、脱敏测试通过；不创建 HTTP client/subprocess。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T6：生成稳定名称并校验 schema

- [ ] 前置依赖：T3。
- [ ] 文件变更核对：newcode/mcp/naming.py；newcode/mcp/config.py；tests/test_mcp_naming.py；tests/test_mcp_config.py
- [ ] 实现核对点：生成 mcp namespace；校验 ASCII、64 字符、slug 截断/digest 保留；轻量 inputSchema 校验。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_naming.py tests/test_mcp_config.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase2"
- [ ] 完成证据：稳定性、collision、边界、schema 错误测试通过；不注册 Tool、不引入 validator。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

## Phase 3：进入与退出门

进入条件：
- [ ] Phase 2 退出门已满足。

退出门：
- [ ] T7-T9 通过；stdio discovery、分页、cache、shutdown 可独立验证。

本 Phase 禁止事项：
- [ ] 不得接入 Registry、Mode、AgentLoop、Permission 或 CLI。

### T7：创建 async runtime bridge

- [ ] 前置依赖：Phase 1、2。
- [ ] 文件变更核对：newcode/mcp/runtime.py；tests/test_mcp_manager.py
- [ ] 实现核对点：使 runtime 成为 event loop、SDK context、进程/client 的唯一 owner，提供幂等 shutdown。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase3"
- [ ] 完成证据：执行/异常/重复关闭通过；未接入 Registry/AgentLoop/CLI。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T8：实现 stdio discovery

- [ ] 前置依赖：T7。
- [ ] 文件变更核对：newcode/mcp/manager.py；newcode/mcp/types.py；tests/test_mcp_manager.py
- [ ] 实现核对点：每 server 独立状态/session/lock；用 SDK 公共 stdio API 发现 tools，输出 descriptor。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase3"
- [ ] 完成证据：成功/启动失败/协议失败/list 失败隔离通过；不注册 Tool。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T9：补齐分页、stdio cache 与关闭

- [ ] 前置依赖：T8。
- [ ] 文件变更核对：newcode/mcp/manager.py；tests/test_mcp_manager.py
- [ ] 实现核对点：按 SDK 公共分页 API 收集 tools；重用 session/cache；关闭 stdio context。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase3"
- [ ] 完成证据：多页、重复 discovery、多 server、重复 shutdown 通过；不实现 HTTP/call。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

## Phase 4：进入与退出门

进入条件：
- [ ] Phase 3 退出门已满足，且 HTTP 公开 API 已在 Phase 1 验证。

退出门：
- [ ] T10-T12 通过；manager 可独立管理 stdio/HTTP、多 server、call、cache、cleanup。

本 Phase 禁止事项：
- [ ] 不得接入 Registry、Permission、AgentLoop 或 CLI；不得真实联网。

### T10：实现 Streamable HTTP discovery

- [ ] 前置依赖：Phase 3；Phase 1 HTTP API 已验证。
- [ ] 文件变更核对：newcode/mcp/runtime.py；newcode/mcp/manager.py；tests/test_mcp_manager.py
- [ ] 实现核对点：按锁定 SDK 创建独立 HTTP transport/client，私有注入 headers，发现失败隔离。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase4"
- [ ] 完成证据：fake HTTP headers、成功/失败、stdio/HTTP 隔离通过；不真实联网。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T11：实现 manager tools/call

- [ ] 前置依赖：T10。
- [ ] 文件变更核对：newcode/mcp/manager.py；tests/test_mcp_manager.py
- [ ] 实现核对点：实现同步 call；同 server 串行；映射 success/isError/exception/unavailable；无自动重连。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase4"
- [ ] 完成证据：call 状态、cache、脱敏与隔离通过；不直接生成 ToolResult/不接 AgentLoop。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T12：实现多 server cleanup

- [ ] 前置依赖：T11。
- [ ] 文件变更核对：newcode/mcp/manager.py；tests/test_mcp_manager.py
- [ ] 实现核对点：汇总安全 status；逐 server cleanup；一个关闭失败不阻断其余；重复关闭安全。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase4"
- [ ] 完成证据：混合 transport、关闭失败、重复关闭通过；不接 CLI。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

## Phase 5：进入与退出门

进入条件：
- [ ] Phase 4 退出门已满足。

退出门：
- [ ] T13-T15 通过；Do schema 可包含 MCP，Plan schema 仍仅三个内置只读工具。

本 Phase 禁止事项：
- [ ] 禁止 CLI 暴露或执行 MCP tool；禁止在 Phase 6 前打通 CLI MCP；禁止把 MCP 判为只读/并发。

### T13：实现 MCPToolAdapter

- [ ] 前置依赖：Phase 4。
- [ ] 文件变更核对：newcode/mcp/adapter.py；newcode/mcp/__init__.py；tests/test_mcp_adapter.py
- [ ] 实现核对点：实现 Tool Protocol；descriptor 映射 spec；manager result 映射 JSON-safe ToolResult 与安全 metadata。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_adapter.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase5"
- [ ] 完成证据：success/isError/exception/脱敏通过；不导入 Provider/Permission/AgentLoop，不接 CLI。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T14：扩展 Registry 分类和冲突防线

- [ ] 前置依赖：T13。
- [ ] 文件变更核对：newcode/tools/registry.py；tests/test_tools_registry.py；tests/test_agent_scheduler.py；tests/test_mcp_adapter.py
- [ ] 实现核对点：增加分类/Do-visible 属性；保留六内置分类；MCP side-effect；检测各类 collision。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_tools_registry.py tests/test_agent_scheduler.py tests/test_mcp_adapter.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase5"
- [ ] 完成证据：内置不变、MCP 串行、冲突拒绝通过；CLI 仍不可见。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T15：扩展 Do Mode 与 Plan gate

- [ ] 前置依赖：T14。
- [ ] 文件变更核对：newcode/agent/mode.py；newcode/agent/loop.py；tests/test_agent_modes.py；tests/test_agent_loop.py；tests/test_tools_registry.py
- [ ] 实现核对点：Plan 固定三内置；Do 由 Registry 得到内置加 external；Plan MCP 为 disallowed 且零远端调用。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_agent_modes.py tests/test_agent_loop.py tests/test_tools_registry.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase5"
- [ ] 完成证据：Plan/Do schema 与六工具回归通过；禁止 CLI MCP 接入直到 Phase 6 完成。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

## Phase 6：进入与退出门

进入条件：
- [ ] Phase 5 退出门已满足。

退出门：
- [ ] T16-T19 通过；MCP 无法绕过 PermissionManager，trusted 下未知 MCP 仍需确认。

本 Phase 禁止事项：
- [ ] 禁止修改 Provider；禁止 Permission 直连 MCP；禁止声称 sandbox 可约束任意远端参数；禁止 CLI MCP 接入。

### T16：扩展 MCP normalizer 与权限类型

- [ ] 前置依赖：Phase 5。
- [ ] 文件变更核对：newcode/permissions/types.py；newcode/permissions/normalizer.py；tests/test_mcp_permissions.py；tests/test_permissions_types.py
- [ ] 实现核对点：生成 mcp_server/tool/transport/arguments；保留非 MCP 请求行为；不猜测远端 path。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_permissions.py tests/test_permissions_types.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase6"
- [ ] 完成证据：normalized args 与非 MCP 回归通过；不改 Provider/transport。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T17：扩展 YAML matcher 与 session allow

- [ ] 前置依赖：T16。
- [ ] 文件变更核对：newcode/permissions/types.py；newcode/permissions/rules.py；newcode/permissions/session.py；tests/test_permissions_rules.py；tests/test_mcp_permissions.py
- [ ] 实现核对点：支持 MCP server/tool exact/glob；严格 YAML 字段；为 MCP 创建精确 session allow。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_permissions_rules.py tests/test_mcp_permissions.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase6"
- [ ] 完成证据：allow/deny/glob/session 精确匹配与 command/path 回归通过；不绕过更高层。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T18：增加 conservative MCP policy

- [ ] 前置依赖：T17。
- [ ] 文件变更核对：newcode/permissions/builtins.py；newcode/permissions/manager.py；newcode/permissions/confirmer.py；tests/test_mcp_permissions.py；tests/test_permissions_manager.py；tests/test_cli_permissions.py
- [ ] 实现核对点：在 explicit rules 后、mode 前要求未知 MCP 高风险确认；摘要仅显示安全 identity。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_mcp_permissions.py tests/test_permissions_manager.py tests/test_cli_permissions.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase6"
- [ ] 完成证据：四 mode、explicit allow/deny、once/session/deny/脱敏通过；不削弱内置策略。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T19：验证 AgentLoop 权限回灌

- [ ] 前置依赖：T18。
- [ ] 文件变更核对：newcode/agent/loop.py；tests/test_agent_loop_mcp.py；tests/test_agent_loop_permissions.py；tests/test_agent_loop.py
- [ ] 实现核对点：scheduler 前检查；deny 按 index 回灌 permission_denied；deny/disallowed 零 adapter/manager 调用；allow 串行。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_agent_loop_mcp.py tests/test_agent_loop_permissions.py tests/test_agent_loop.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase6"
- [ ] 完成证据：deny/allow/session/Plan/顺序回灌通过；Phase 6 前不接 CLI。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

## Phase 7：进入与退出门

进入条件：
- [ ] Phase 6 退出门已满足。

退出门：
- [ ] T20-T23 通过；CLI 是唯一 MCP lifecycle owner，获准 MCP 可执行、拒绝时远端零调用。

本 Phase 禁止事项：
- [ ] 禁止在 run_conversation 写 transport；禁止向 Prompt/session 注入 server/URL/header/env/status/error；禁止改变命令/session 语义或新增本章外 MCP 功能。

### T20：接入 CLI startup 与 discovery

- [ ] 前置依赖：Phase 6。
- [ ] 文件变更核对：newcode/cli.py；tests/test_cli_mcp.py；必要时 tests/test_cli_agent_loop.py、tests/test_cli.py
- [ ] 实现核对点：main 加载独立 config、创建 manager、逐 server discovery、注册 adapter、脱敏 warning。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_cli_mcp.py tests/test_cli_agent_loop.py tests/test_cli.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase7"
- [ ] 完成证据：无 config/成功/失败隔离/Plan-Do schema 通过；run_conversation 不写 transport。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T21：实现 CLI finally shutdown

- [ ] 前置依赖：T20。
- [ ] 文件变更核对：newcode/cli.py；tests/test_cli_mcp.py；tests/test_cli.py
- [ ] 实现核对点：startup 后用 try/finally 包住会话；EOF、exit、中断、异常都幂等 shutdown。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_cli_mcp.py tests/test_cli.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase7"
- [ ] 完成证据：所有退出路径 cleanup 通过；不改命令/session 语义。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T22：补最小 Prompt 权限提醒

- [ ] 前置依赖：T20；仅当前 Prompt 缺少该提醒时。
- [ ] 文件变更核对：newcode/prompt/modules.py；必要时 newcode/prompt/reminder.py；tests/test_prompt_builder.py；tests/test_prompt_reminder.py
- [ ] 实现核对点：仅提示外部工具受权限检查；不注入 server、URL、headers、env、状态、错误、secret。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_prompt_builder.py tests/test_prompt_reminder.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase7"
- [ ] 完成证据：提醒/不污染 session 通过；Prompt 不承载 transport/安全决策。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T23：CLI 端到端 MCP 工具流回归

- [ ] 前置依赖：T20-T22。
- [ ] 文件变更核对：tests/test_cli_mcp.py；tests/test_cli_agent_loop.py；tests/test_cli_tool_flow.py；tests/test_cli_permissions.py
- [ ] 实现核对点：fake manager/adapter/provider 验证 Do 调用、结果回灌、最终回答、Plan 零远端、确认 allow/deny、DSML 不泄露。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_cli_mcp.py tests/test_cli_agent_loop.py tests/test_cli_tool_flow.py tests/test_cli_permissions.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase7"
- [ ] 完成证据：CLI 主链路与六工具回归通过；不做本章外 MCP feature。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

## Phase 8：进入与退出门

进入条件：
- [ ] Phase 1-7 的 compileall 与 targeted pytest 均通过。

退出门：
- [ ] T24-T26 通过；边界审计、全量 pytest 和人工验收有证据。

本 Phase 禁止事项：
- [ ] 不得为审计或测试重构无关模块、放宽安全边界或新增工具。

### T24：审计模块边界与 scope

- [ ] 前置依赖：Phase 1-7 全部通过。
- [ ] 文件变更核对：原则上无；必要时最小回归文件；tests/test_provider_tool_calls.py；tests/test_deepseek_provider.py；tests/test_tools_registry.py
- [ ] 实现核对点：检查 import 边界、无 SSE/cache_control/reconnect/额外 MCP feature、内置工具未变化。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest tests/test_provider_tool_calls.py tests/test_deepseek_provider.py tests/test_tools_registry.py -q --basetemp "${env:TEMP}\newcode-pytest-mcp-phase8"
- [ ] 完成证据：审计与回归通过；不重构无关模块。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T25：运行全量自动回归

- [ ] 前置依赖：T24。
- [ ] 文件变更核对：原则上无；失败时仅最小相关文件。
- [ ] 实现核对点：先重跑失败 targeted tests，随后 compileall 与全量 pytest，使用系统 TEMP basetemp。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest -q --basetemp "${env:TEMP}\newcode-pytest-mcp"
- [ ] 完成证据：全量 pytest 通过，安全/Provider/Plan-Do/DSML/Prompt/六工具无回归；不放宽任何边界。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

### T26：执行真实 CLI 人工验收

- [ ] 前置依赖：T25；准备无生产 secret 的受控 stdio/HTTP fixture。
- [ ] 文件变更核对：无；如发现 bug 先补最小自动测试。
- [ ] 实现核对点：验收无 config、stdio discovery、失败隔离、Do 获准调用、Plan 零远端、trusted 确认、exit 释放资源。
- [ ] 必跑 compileall：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 必跑 targeted pytest：.\.venv\Scripts\python.exe -m pytest -q --basetemp "${env:TEMP}\newcode-pytest-mcp"
- [ ] 完成证据：每项人工结果有记录；失败有可复现自动测试；不接 OAuth/生产 secret/超范围能力。
- [ ] 边界确认：未越过本任务或本 Phase 的禁止事项；未修改未列出的无关模块。

## Phase 8 全量回归与人工验收清单

- [ ] Provider function calling、DSML 分片与 allow_tool_calls=False 拦截回归通过。
- [ ] 普通聊天、内置单次/多轮工具调用、Plan/Do、Permission、Prompt session 不污染回归通过。
- [ ] MCP stdio 与 Streamable HTTP discovery/call 的自动测试通过；无效 server 不影响内置工具。
- [ ] MCP 名称、schema、collision、串行调度、Do 可见性、Plan 拒绝均有测试证据。
- [ ] strict/default/permissive/trusted 下未知 MCP 都要求确认；explicit allow、session allow、deny 均有证据。
- [ ] permission_denied 会回灌模型且远端零调用。
- [ ] CLI 在 EOF、/exit、KeyboardInterrupt、异常均 cleanup。
- [ ] 人工验收不用生产 secret，不执行 OAuth 或本章范围外能力。
- [ ] 最终命令：.\.venv\Scripts\python.exe -m compileall newcode
- [ ] 最终命令：.\.venv\Scripts\python.exe -m pytest -q --basetemp "${env:TEMP}\newcode-pytest-mcp"
