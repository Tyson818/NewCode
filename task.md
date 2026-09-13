# Chapter 7：MCP Client 任务拆分

## 全局执行约束

- 本文只拆分任务，不代表已进入实现。
- 不得手写 JSON-RPC、request id、pending correlation 或 legacy SSE。
- 不得新增 MCP resources、prompts、sampling、tasks、Apps、自动重连、复杂 OAuth、network sandbox 或 MCP 专属 UI。
- Provider 不得导入 newcode.mcp、Permission 或 Prompt；MCP 模块不得导入 Provider；AgentLoop 不得直接使用 SDK transport。
- 六个内置工具名称、schema、分类、执行行为及 run_command 安全边界不得改变。
- 所有 MCP tool 固定为有副作用、Do Mode 可见、串行执行；Plan Mode 不可见。
- MCP config、env、headers、secret、连接细节和远端错误不得写入 session、prompt、ToolResult metadata 或普通日志。
- 每个 Phase 完成后必须运行 compileall 与该 Phase 的 targeted pytest；通过后才能进入下一 Phase。
- 使用系统临时目录作为 pytest basetemp，例如 "$env:TEMP\newcode-pytest-<phase>"，不得使用仓库内相对 basetemp 目录。

## Phase 1：SDK 版本锁定与 API 验证

### T1：选择并锁定官方 MCP Python SDK

前置条件：
- Chapter 7 spec.md 与 plan.md 已批准。
- 当前环境尚未安装 MCP SDK。

修改/新增文件：
- pyproject.toml

实现动作：
- 调研并选定满足 Python 版本约束的官方 MCP Python SDK。
- 将依赖锁定为明确版本，不使用无上限版本范围。
- 不引入 JSON Schema validator 或其他与本章无关依赖。
- 记录本章依赖的公开 API：stdio、Streamable HTTP、tools/list、tools/call、关闭和兼容模式。

对应测试：
- tests/test_mcp_sdk_compatibility.py 的 import/version 断言。

完成判定：
- SDK 已声明为固定版本。
- 未修改任何 Provider、AgentLoop、Registry、Permission、CLI 或 MCP runtime 文件。

不得跨越的边界：
- 不实现 transport。
- 不以私有 SDK API 或手写 JSON-RPC 补齐缺失能力。

### T2：建立 SDK 公开 API 兼容测试

前置条件：
- T1 已完成，SDK 可在项目环境安装和导入。

修改/新增文件：
- tests/test_mcp_sdk_compatibility.py

实现动作：
- 用 SDK 官方测试支持、fake 或最小 fixture 验证公开 API。
- 覆盖 stdio 参数入口、HTTP headers 注入入口、tools/list 分页形态、tools/call 成功/isError、async context cleanup。
- 验证 SDK 的公开兼容模式，而不假设传统 initialize 是 NewCode 固定步骤。
- 若 API 不满足 spec，停止并回到 T1 调整版本或提出规格修订。

对应测试：
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_sdk_compatibility.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase1"

完成判定：
- 兼容测试覆盖所有本章必需 SDK 表面。
- Phase 1 未通过时，不得进入任何依赖 SDK 的后续工作。

不得跨越的边界：
- 不创建 MCPManager、adapter 或 CLI 接入。
- 不访问真实外网或第三方 MCP Server。

## Phase 2：配置、命名与 schema 纯逻辑

### T3：定义 MCP 核心类型与安全错误表示

前置条件：
- Phase 1 已通过。

修改/新增文件：
- newcode/mcp/__init__.py
- newcode/mcp/types.py
- tests/test_mcp_config.py

实现动作：
- 定义 MCPServerConfig、MCPServerStatus、MCPToolDescriptor。
- 定义可按 server 收集的 MCP 配置/发现错误表示。
- 定义私有敏感值持有方式，避免 dataclass repr、错误和状态对象泄露展开值。

对应测试：
- 核心类型构造、字段读取、错误按 server 隔离和安全 repr。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_config.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase2"

完成判定：
- 类型模块不导入 Provider、AgentLoop、PermissionManager、CLI 或 MCP SDK runtime。

不得跨越的边界：
- 不读取文件、不启动 server、不写配置文件。

### T4：实现独立 MCP 配置加载与 user/project 合并

前置条件：
- T3 已完成。

修改/新增文件：
- newcode/mcp/config.py
- tests/test_mcp_config.py

实现动作：
- 读取 ~/.newcode/mcp.yaml 与 workspace/.newcode/mcp.yaml。
- 支持顶层 mcp_servers map。
- 按 user 到 project 合并；项目同名 server 完整覆盖用户条目。
- 校验 server key、transport、stdio command/args/env、HTTP url/headers 与字段类型。
- 空或缺失 MCP config 视为无 MCP server，不是启动错误。

对应测试：
- 无文件、空文件、用户配置、项目覆盖、多个 server、未知 transport、字段类型错误。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_config.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase2"

完成判定：
- 配置错误仅归属对应 server，其他配置仍可用。

不得跨越的边界：
- 不修改既有 AppConfig/load_config 的 config.yaml 行为。
- 不接入 CLI。

### T5：实现变量展开、stdio 环境策略与 secret 脱敏输入

前置条件：
- T4 已完成。

修改/新增文件：
- newcode/mcp/config.py
- tests/test_mcp_config.py

实现动作：
- 展开 env/header 文本中的一个或多个 ${VAR}。
- 变量缺失或空值时将该 server 标记 mcp_config_error，不保留或发送占位符。
- 明确 stdio 最终环境为当前进程环境加显式 env 覆盖。
- 将展开值写入仅供 runtime 使用的敏感值集合。

对应测试：
- 多变量展开、缺失/空变量、headers/env 两类输入、异常文本与对象 repr 不泄露 secret。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_config.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase2"

完成判定：
- 不存在未展开 placeholder 被送入有效 server 配置。

不得跨越的边界：
- 不创建 HTTP client 或 subprocess。
- 不把 secret 加入 ToolContext.sensitive_values 以外的公共状态。

### T6：实现稳定 MCP 工具命名与轻量 schema 校验

前置条件：
- T3 已完成。

修改/新增文件：
- newcode/mcp/naming.py
- newcode/mcp/config.py 或独立 schema helper
- tests/test_mcp_naming.py
- tests/test_mcp_config.py

实现动作：
- 生成 mcp__server_slug__tool_slug__identity_digest。
- 主动校验最终名仅含 ASCII [A-Za-z0-9_-] 且最多 64 字符。
- 只截断 slug，固定 digest 不截断。
- 校验 inputSchema 是 object，type 为 object 或省略，关键成员可 JSON 转发。
- 产出稳定的 schema/name 错误码。

对应测试：
- 跨调用稳定性、不同 server 同名 tool、slug collision、非法名称、64 字符边界。
- schema 合法、type 错误、非 object、不可转发字段。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_naming.py tests/test_mcp_config.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase2"

完成判定：
- 不引入 JSON Schema validator 新依赖。
- 纯函数可独立测试。

不得跨越的边界：
- 不向 ToolRegistry 注册任何工具。

## Phase 3：stdio runtime、manager 与 discovery

### T7：实现受控 async runtime bridge

前置条件：
- Phase 1 与 Phase 2 已通过。

修改/新增文件：
- newcode/mcp/runtime.py
- tests/test_mcp_manager.py

实现动作：
- 创建唯一的同步到异步 runtime bridge。
- 规定 runtime 是 event loop、SDK context、stdio subprocess 和后续 HTTP client 的唯一 owner。
- 提供可测试、可幂等 shutdown 的生命周期接口。

对应测试：
- 创建、单次执行、异常、重复 shutdown。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase3"

完成判定：
- adapter 或未来调用方无需自行创建 event loop。

不得跨越的边界：
- 不实现 Registry/AgentLoop/CLI 接入。
- 不用私有 SDK API。

### T8：创建 MCPManager stdio connection 与单 server discovery

前置条件：
- T7 已完成。
- Phase 1 的 stdio API 测试已通过。

修改/新增文件：
- newcode/mcp/manager.py
- newcode/mcp/types.py
- tests/test_mcp_manager.py

实现动作：
- 为每个 server 建立独立状态、SDK context、session、lock 和安全错误摘要。
- 使用锁定 SDK 的 stdio 公共 API 启动 server。
- 以公开 API 执行 protocol compatibility 和 tools/list。
- 将远端结果转为 MCPToolDescriptor，暂不适配/注册。

对应测试：
- 成功 discovery、启动 command 失败、协议错误、tools/list 错误。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase3"

完成判定：
- 单 server failure 不会终止 manager 对其他 server 的工作。

不得跨越的边界：
- 不让 CLI 启动 manager。
- 不注册 Tool。

### T9：完善 tools/list 分页、stdio cache 与关闭

前置条件：
- T8 已完成。

修改/新增文件：
- newcode/mcp/manager.py
- tests/test_mcp_manager.py

实现动作：
- 按锁定 SDK 的公开分页模式收集完整 tool 列表。
- 为同 server discovery 重用 session/cache，不重复启动 subprocess。
- 同一 server 保留串行 lock。
- shutdown 关闭 stdio context 并清空 cache。

对应测试：
- 多页列表、重复 discovery、多个 stdio server、一个 server failure isolation、重复 shutdown。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase3"

完成判定：
- stdio lifecycle 可独立人工验收：启动、发现、关闭，无遗留子进程。

不得跨越的边界：
- 不实现 HTTP 或 tools/call。

## Phase 4：HTTP、多 server、call 和 cleanup

### T10：实现 Streamable HTTP connection 与 discovery

前置条件：
- Phase 3 已通过。
- Phase 1 已验证 HTTP 公开 API 与 headers 注入方式。

修改/新增文件：
- newcode/mcp/runtime.py
- newcode/mcp/manager.py
- tests/test_mcp_manager.py

实现动作：
- 创建每 server 独立 HTTP client/transport。
- 仅在私有 runtime 注入已展开 headers。
- 复用现有 discovery descriptor 流程。
- HTTP discovery failure 仅使该 server unavailable。

对应测试：
- fake HTTP transport/endpoint、headers 注入、HTTP discovery 成功/失败、stdio+HTTP 隔离。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase4"

完成判定：
- 无真实网络依赖，HTTP client 可独立关闭。

不得跨越的边界：
- 不修改 CLI、Registry 或 Permission。

### T11：实现 manager tools/call、cache 和失败映射

前置条件：
- T10 已完成。

修改/新增文件：
- newcode/mcp/manager.py
- tests/test_mcp_manager.py

实现动作：
- 提供 call_tool_sync(server, remote_name, arguments)。
- 通过 runtime bridge 调用 SDK tools/call。
- 区分 success、isError、SDK/transport exception、unavailable。
- 同 server call 串行，连接失效不自动 reconnect。
- 不泄露 headers/env/URL credentials/stack trace。

对应测试：
- success、isError、异常、unavailable、同 server cache、不同 server 隔离。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase4"

完成判定：
- MCP manager failure 仅作为受控 MCP 层结果返回。

不得跨越的边界：
- 不直接生成 ToolResult，不接入 AgentLoop。

### T12：实现多 server 幂等 shutdown 与状态报告

前置条件：
- T11 已完成。

修改/新增文件：
- newcode/mcp/manager.py
- tests/test_mcp_manager.py

实现动作：
- 汇总安全 MCPServerStatus。
- 逐 server cleanup；一个 cleanup 失败不阻塞其余。
- 清理 HTTP clients、stdio contexts、subprocess 与 cache。
- 支持 shutdown 多次调用。

对应测试：
- 混合 transport、多 server、一个关闭失败、重复关闭、无 secret 的状态和错误摘要。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase4"

完成判定：
- manager 本身完成且仍未接入 CLI。

不得跨越的边界：
- 不增加自动重连或 health checking。

## Phase 5：Adapter、Registry 与 Mode

### T13：实现 MCPToolAdapter 与 ToolResult 映射

前置条件：
- Phase 4 已通过。

修改/新增文件：
- newcode/mcp/adapter.py
- newcode/mcp/__init__.py
- tests/test_mcp_adapter.py

实现动作：
- 实现现有 Tool Protocol 的 spec/run。
- 将 descriptor 的 name、description、inputSchema 映射为 ToolSpec。
- 将 manager call success/isError/异常映射为 JSON-safe ToolResult。
- metadata 仅含安全 mcp_server、mcp_tool、transport identity。

对应测试：
- schema、arguments、success、isError、exception、无 SDK 原始对象和无 secret。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_adapter.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase5"

完成判定：
- adapter 不导入 Provider、Permission 或 AgentLoop。

不得跨越的边界：
- 不将 adapter 注册进 CLI 或 Provider。

### T14：扩展 ToolRegistry 分类和冲突防线

前置条件：
- T13 已完成。

修改/新增文件：
- newcode/tools/registry.py
- tests/test_tools_registry.py
- tests/test_agent_scheduler.py

实现动作：
- 在不改变六内置分类的前提下，为注册工具维护显式 read-only/side-effect 与 Do-visible 属性。
- MCP adapter 固定为 side-effect、Do-visible。
- 保留 register 的重复 ValueError 作为最后防线。
- registry/manager 在注册前检测内置冲突、稳定名冲突和 digest collision。

对应测试：
- 内置 schema 与分类不变、MCP 非只读、冲突拒绝、scheduler 对 MCP 串行。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_tools_registry.py tests/test_agent_scheduler.py tests/test_mcp_adapter.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase5"

完成判定：
- MCP tool 可存在 registry，但尚不得通过 CLI 调用。

不得跨越的边界：
- 不改变六个实际工具行为。

### T15：扩展 Do Mode schema 与 Plan Mode gate

前置条件：
- T14 已完成。

修改/新增文件：
- newcode/agent/mode.py
- newcode/agent/loop.py
- tests/test_agent_modes.py
- tests/test_agent_loop.py

实现动作：
- 保持 Plan Mode 固定三个内置只读工具。
- 使 Do Mode 通过 Registry 导出六内置加 Do-visible external tools，不继续以固定 DO_TOOL_NAMES 代表完整集合。
- 保持模型伪造 Plan Mode MCP call 为 disallowed_tool，且不进入 permission/manager。
- 保持 unknown_tool 的既有行为。

对应测试：
- Plan/Do schema、Plan MCP disallowed、Do MCP 可见、六工具回归。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_agent_modes.py tests/test_agent_loop.py tests/test_tools_registry.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase5"

完成判定：
- Phase 5 只完成 schema/loop 可见性；不得让 CLI 暴露或执行 MCP 工具。

不得跨越的边界：
- Phase 6 未完成前，禁止进行 CLI MCP 调用接入。

## Phase 6：MCP Permission 扩展

### T16：扩展 MCP identity normalizer 与 Permission 数据类型

前置条件：
- Phase 5 已通过。

修改/新增文件：
- newcode/permissions/types.py
- newcode/permissions/normalizer.py
- tests/test_mcp_permissions.py
- tests/test_permissions_types.py

实现动作：
- 从 MCP adapter/ToolCall 获取 mcp_server、mcp_tool、mcp_transport、mcp_arguments。
- 保持原始 args 与 normalized args 的现有语义。
- 不把远端 arbitrary arguments 误当成本地 path sandbox 输入。

对应测试：
- MCP normalized args、非 MCP 工具不变、无 secret 输出。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_permissions.py tests/test_permissions_types.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase6"

完成判定：
- 当前 hard denylist/workspace sandbox 的适用范围没有被虚假扩大。

不得跨越的边界：
- 不修改 Provider 或 MCP transport。

### T17：扩展 YAML rule matcher 与 session allow

前置条件：
- T16 已完成。

修改/新增文件：
- newcode/permissions/types.py
- newcode/permissions/rules.py
- newcode/permissions/session.py
- tests/test_permissions_rules.py
- tests/test_mcp_permissions.py

实现动作：
- PermissionMatch 增加 mcp_server/mcp_server_glob/mcp_tool/mcp_tool_glob。
- YAML parser 只接受明确新字段。
- matcher 支持 exact/glob MCP identity。
- session confirmation 可创建精确 MCP server/tool allow rule。

对应测试：
- explicit allow/deny、glob、无效字段、session rule 精确匹配、原 command/path rules 回归。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_permissions_rules.py tests/test_mcp_permissions.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase6"

完成判定：
- session allow 不会因 server/tool 名相似而误放行。

不得跨越的边界：
- 不允许 rule 直接绕过 hard denylist、sandbox 或 Plan Mode。

### T18：增加 conservative MCP built-in policy 与 CLI confirmer 摘要

前置条件：
- T17 已完成。

修改/新增文件：
- newcode/permissions/builtins.py
- newcode/permissions/manager.py
- newcode/permissions/confirmer.py
- tests/test_permissions_manager.py
- tests/test_mcp_permissions.py
- tests/test_cli_permissions.py

实现动作：
- 在 explicit rules 之后、permission mode 之前增加 external MCP built-in policy。
- 所有未显式 allow/deny MCP tools 返回 high-risk require_confirmation。
- strict/default/permissive/trusted 均不能自动 allow 未授权 MCP tool。
- CLI confirmation 安全显示 MCP server/tool；不得显示 secret 或完整 arguments。

对应测试：
- 四种 mode 的 MCP 默认确认、explicit allow/deny 优先级、once/session/deny、CLI 摘要脱敏。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_mcp_permissions.py tests/test_permissions_manager.py tests/test_cli_permissions.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase6"

完成判定：
- 只有 explicit allow 或确认可放行未知 MCP tool。

不得跨越的边界：
- 不削弱已有 hard denylist、workspace sandbox、sensitive file 或内置权限策略。

### T19：验证 AgentLoop 的 MCP permission 回灌

前置条件：
- T18 已完成。

修改/新增文件：
- newcode/agent/loop.py（仅必要的 adapter identity/权限接入调整）
- tests/test_agent_loop_mcp.py
- tests/test_agent_loop_permissions.py

实现动作：
- 确保 MCP call 在 scheduler 前完成 PermissionManager.check。
- deny 时产生 permission_denied ToolResult，按原始 index 写入 session 并回灌下一轮。
- deny/disallowed 时不得调用 adapter/manager。
- 允许后按既有 scheduler 规则串行执行。

对应测试：
- deny、allow、session allow、Plan disallowed、tool result 回灌、顺序稳定。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_agent_loop_mcp.py tests/test_agent_loop_permissions.py tests/test_agent_loop.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase6"

完成判定：
- Phase 6 完整后才允许开展 CLI MCP 调用接入。

不得跨越的边界：
- 不把 permission/MCP 逻辑放入 Provider。

## Phase 7：CLI 生命周期与端到端接入

### T20：实现 CLI MCP startup、discovery 与 registry 注册

前置条件：
- Phase 6 已全部通过。

修改/新增文件：
- newcode/cli.py
- tests/test_cli_mcp.py

实现动作：
- main 创建内置 registry、ToolContext、PermissionManager 后加载独立 MCP config。
- 创建 manager，逐 server discovery，成功 adapter 注册。
- 单 server failure 输出脱敏 warning，不阻止 CLI/内置工具/其他 server。
- 保持 run_conversation 可依赖注入测试。

对应测试：
- 无 config、成功 discovery、单 server failure isolation、Do/Plan schema 行为。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_cli_mcp.py tests/test_cli_agent_loop.py tests/test_cli.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase7"

完成判定：
- CLI 仅在 Phase 6 后才暴露 MCP tool。
- Provider 不修改。

不得跨越的边界：
- 不在 run_conversation 内直接实现 transport。

### T21：实现 CLI finally shutdown 覆盖退出路径

前置条件：
- T20 已完成。

修改/新增文件：
- newcode/cli.py
- tests/test_cli_mcp.py
- tests/test_cli.py

实现动作：
- 将 startup 后 run_conversation 放入 try/finally。
- 覆盖 EOF、/exit、KeyboardInterrupt、交互异常、启动后异常。
- 每个路径调用幂等 MCPManager.shutdown。

对应测试：
- fake manager 验证每种退出路径 cleanup；一个 server cleanup failure 不阻断整体。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_cli_mcp.py tests/test_cli.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase7"

完成判定：
- CLI 是 MCP lifecycle 唯一 owner。

不得跨越的边界：
- 不改变 /plan、/do、/exit 的 session 污染语义。

### T22：补充最小 Prompt 外部工具权限提醒

前置条件：
- T20 已完成。
- 仅当现有 Prompt System 缺少稳定提醒时执行。

修改/新增文件：
- newcode/prompt/modules.py
- 必要时 newcode/prompt/reminder.py
- tests/test_prompt_builder.py
- tests/test_prompt_reminder.py

实现动作：
- 仅增加“外部工具同样受权限检查”的稳定提示。
- 不加入 server name、URL、headers、env、状态、远端错误或 secret。
- 不改变 PromptBuilder/session 污染边界。

对应测试：
- stable prompt 提示存在；system-reminder/session 行为不变。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_prompt_builder.py tests/test_prompt_reminder.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase7"

完成判定：
- Prompt 只是行为提醒，安全判断仍完全由 Permission System 负责。

不得跨越的边界：
- 不把 MCP transport 或 server 配置塞进 Prompt System。

### T23：完成 CLI MCP 端到端工具流回归

前置条件：
- T20-T22 已通过。

修改/新增文件：
- tests/test_cli_mcp.py
- tests/test_cli_tool_flow.py
- tests/test_cli_agent_loop.py
- tests/test_cli_permissions.py

实现动作：
- 用 fake MCP manager/adapter 和 provider 验证 Do Mode MCP tool 执行、ToolResult 回灌和最终自然语言回答。
- 验证 Plan Mode 请求 MCP tool 只产生 disallowed_tool，绝不触发确认或远端调用。
- 验证拒绝、确认允许和 server failure 不影响正常 CLI 流程。
- 验证 DSML 不泄露。

对应测试：
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_cli_mcp.py tests/test_cli_agent_loop.py tests/test_cli_tool_flow.py tests/test_cli_permissions.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase7"

完成判定：
- CLI MCP 主链路完成，且六内置工具回归通过。

不得跨越的边界：
- 不实现 MCP resources/prompts/sampling 或自动 reconnect。

## Phase 8：全量回归与人工验收

### T24：执行模块边界与新增功能审计

前置条件：
- Phase 1-7 targeted tests 全部通过。

修改/新增文件：
- 原则上不修改生产文件。
- 仅在测试揭示回归时作最小修改。

实现动作：
- 检查 Provider 不导入 MCP/Permission/Prompt。
- 检查 MCP 模块不导入 Provider，AgentLoop 不导入 SDK transport。
- 检查无 legacy SSE、cache_control、automatic reconnect、resources/prompts/sampling/tasks/Apps。
- 检查 Registry 仍只有六内置工具加已发现 MCP adapters，未新增内置工具。

对应测试：
- 既有 Provider、Tool Registry、Prompt、Agent、Permission、CLI 测试。
- compileall newcode
- .\.venv\Scripts\python.exe -m pytest tests/test_provider_tool_calls.py tests/test_deepseek_provider.py tests/test_tools_registry.py -q --basetemp "$env:TEMP\newcode-pytest-mcp-phase8"

完成判定：
- 职责边界和 scope guardrails 无违反。

不得跨越的边界：
- 不为通过审计而重构无关模块。

### T25：运行全量自动回归

前置条件：
- T24 已通过。

修改/新增文件：
- 原则上无。
- 若失败，仅修复失败直接证明的最小回归模块及对应测试。

实现动作：
- 执行全量 compileall。
- 执行全量 pytest，使用系统临时目录的明确子目录。
- 失败后先重跑相关 targeted tests，再重跑全量。

对应测试：
- .\.venv\Scripts\python.exe -m compileall newcode
- .\.venv\Scripts\python.exe -m pytest -q --basetemp "$env:TEMP\newcode-pytest-mcp"

完成判定：
- 全量 pytest 通过。
- 不新增工具、不改变六内置基本行为、不把 Prompt/MCP/Permission 策略塞进 Provider。

不得跨越的边界：
- 不因测试方便而放宽 MCP 默认确认、Plan Mode 或 secret 边界。

### T26：执行真实 CLI 人工验收

前置条件：
- T25 已通过。
- 已准备不含生产 secret 的本地 stdio fixture 和可控 HTTP fixture。

修改/新增文件：
- 无；除非人工验收发现可复现的最小 bug。

实现动作：
- 验收无 MCP config 的普通聊天。
- 验收有效 stdio server discovery。
- 验收无效 server 不影响内置工具。
- 验收 Do Mode 获准 MCP tool 调用和最终回答。
- 验收 Plan Mode MCP call 被拒绝且不访问远端。
- 验收 trusted mode 下未知 MCP tool 仍要求确认。
- 验收 /exit 后 stdio/HTTP 资源释放。

对应测试：
- 人工 CLI 命令与受控 fixture。
- 若发现问题，先补最小自动化回归测试，再修复。

完成判定：
- Chapter 7 验收项全部具备自动或人工证据。

不得跨越的边界：
- 不在人工验收中接入真实生产 secret、复杂 OAuth 或未在 spec 内的 MCP 能力。
