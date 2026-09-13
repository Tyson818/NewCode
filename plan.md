# Chapter 7：MCP Client 实施计划

## 计划目标

本计划按依赖关系将 NewCode MCP Client 拆为可独立测试、可人工验收的小阶段。完成后，CLI 在启动时从独立 MCP 配置发现 stdio 与 Streamable HTTP server 的 tools，成功工具经 adapter 注册到现有 Tool Registry，并继续受到 Plan/Do、Permission System、ToolScheduler 和 AgentLoop 的约束。

本章只实现 MCP tools。不实现 JSON-RPC、legacy SSE、resources、prompts、sampling、tasks、Apps、自动重连、复杂 OAuth、network sandbox 或 MCP 专属 UI。

## 固定边界

- Provider 只处理 Chat Completions messages/tools 与 ProviderEvent，不导入 MCP、Prompt 或 Permission。
- MCPManager/adapter 不负责 AgentLoop 编排、Prompt 策略或 Permission 决策。
- AgentLoop 不实现 MCP transport、SDK lifecycle 或 JSON-RPC。
- 所有 MCP tool 经 AgentLoop -> Plan/Do gate -> PermissionManager -> ToolScheduler -> executor -> adapter。
- 六个内置工具的名称、schema、分类和基本行为不变。
- MCP tool 固定为有副作用、仅 Do Mode 可见、串行调度。
- 未命中 explicit allow/deny 的 MCP tool 在 strict/default/permissive/trusted 下都 require_confirmation。
- MCP config、headers、展开后的 env、连接细节与 secret 不进入 session、prompt、ToolResult metadata 或普通日志。

## Phase 总览

| Phase | 内容 | 改动边界 |
|---|---|---|
| 1 | MCP SDK 版本锁定和公开 API 验证 | 依赖与测试，尚不写 MCP runtime |
| 2 | 配置、命名、schema 校验 | 仅新增 newcode/mcp 纯逻辑 |
| 3 | stdio runtime、manager、discovery | 仅新增 MCP 生命周期模块 |
| 4 | Streamable HTTP、多 server、cache、shutdown | 仅新增 MCP 生命周期模块 |
| 5 | Adapter、Registry 分类、Mode 可见性 | 扩展 tools/agent mode |
| 6 | MCP Permission、规则和 session allow | 扩展 permissions/agent |
| 7 | CLI startup/shutdown、端到端接入 | 扩展 CLI，最小 Prompt 提醒 |
| 8 | 全量回归与人工验收 | 仅最小回归修复 |

## Phase 1：MCP SDK 版本锁定与公开 API 验证

### 目标

在任何 transport 实现前，选定并锁定官方 MCP Python SDK 版本，以测试确认该版本公开 API 支持 stdio、Streamable HTTP、tools/list、分页、tools/call、关闭和协议兼容。

### 涉及文件

- 修改 pyproject.toml。
- 新增 tests/test_mcp_sdk_compatibility.py。
- 不创建 MCPManager、adapter，不修改 Provider、AgentLoop、Registry、Permission 或 CLI。

### 核心设计

- 只使用官方 MCP Python SDK，禁止手写 JSON-RPC。
- 将 SDK 固定到明确版本，避免 API 漂移。
- 用 SDK 官方测试辅助或 fake session/transport 验证公开入口：
  - stdio 参数和 client/session 创建方式；
  - Streamable HTTP transport 与 headers 注入位置；
  - tools/list 的结果与分页形态；
  - tools/call 成功、isError、异常；
  - async context 的关闭；
  - 传统与现代 server 的公开兼容模式。
- 若锁定版本不能满足某项需求，停在本 Phase，调整 SDK 版本或修订规格；不得使用私有 API 或手写 fallback。

### 测试范围

- SDK 可导入且版本符合锁定值。
- 全部兼容测试不访问真实网络或第三方 server。
- 测试记录后续 runtime 必须使用的公开 API 契约。

### 完成标准

- pyproject.toml 已锁定官方 MCP SDK。
- 公开 API 兼容测试通过。
- 没有 MCP 功能接入现有运行链路。

### 验收命令

    .\.venv\Scripts\python.exe -m pytest tests/test_mcp_sdk_compatibility.py -q

### 风险

SDK 可能要求更高 Python 版本，或其 Streamable HTTP API 与预期不同；本阶段是后续实现的阻断点。

## Phase 2：MCP 配置、命名与 schema 纯逻辑

### 目标

建立无需连接 server 的 MCP 配置、合并、变量展开、敏感值管理、稳定命名与轻量 schema 校验。

### 涉及文件

新增：

- newcode/mcp/__init__.py
- newcode/mcp/types.py
- newcode/mcp/config.py
- newcode/mcp/naming.py
- tests/test_mcp_config.py
- tests/test_mcp_naming.py

不修改 Tool Registry、AgentLoop、Permission、CLI。

### 核心设计

- 定义 MCPServerConfig、MCPServerStatus、MCPToolDescriptor 及配置错误类型。
- 独立加载 ~/.newcode/mcp.yaml 与 workspace/.newcode/mcp.yaml。
- 顶层为 mcp_servers map；项目同名 server 完整覆盖用户条目，不做字段级 merge。
- 校验 stdio 的 command/args/env，及 Streamable HTTP 的 url/headers。
- 支持一个或多个 ${VAR} 展开；缺失或空值只使对应 server 成为 mcp_config_error，不发送原样占位符。
- stdio 运行时环境的最终策略为继承当前进程环境，再由已验证、已展开 env 覆盖。
- 展开 secret 仅通过私有敏感值通道传给 runtime，禁止进入配置错误、repr、日志或 ToolResult。
- 命名纯函数生成 mcp__server_slug__tool_slug__digest；最终名主动校验 ASCII [A-Za-z0-9_-] 和最大 64 字符；只截断 slug，不截断 digest。
- inputSchema 采用轻量结构校验：schema 是 object，type 为 object 或省略，properties/required/additionalProperties 如存在必须可 JSON 转发；不引入 JSON Schema validator。

### 测试范围

- 空、用户、项目配置及完整覆盖合并。
- stdio/HTTP 合法和非法字段。
- 多变量、缺失变量、空变量和脱敏错误。
- 名称稳定性、同名远端工具、slug 碰撞、非法字符与 64 字符边界。
- schema 合法/不合法形态。

### 完成标准

- 纯逻辑模块不导入 MCP SDK、Provider、AgentLoop、PermissionManager 或 CLI。
- 错误按 server 收集，其他 server 可继续。
- 命名和 schema 校验完全由单元测试覆盖。

### 验收命令

    .\.venv\Scripts\python.exe -m pytest tests/test_mcp_config.py tests/test_mcp_naming.py -q

### 风险

配置错误、dataclass repr 和测试断言都是 secret 泄露路径，必须专项测试。

## Phase 3：stdio runtime、MCPManager 骨架与 discovery

### 目标

实现受控 async runtime bridge、单 server stdio 连接和 tools/list discovery，暂不注册为 NewCode Tool，也不接入 CLI。

### 涉及文件

新增：

- newcode/mcp/runtime.py
- newcode/mcp/manager.py
- tests/test_mcp_manager.py

可修改 newcode/mcp/types.py 以补充 runtime/discovery 状态。

### 核心设计

- MCPManager 是每 server async runtime、SDK context、session、lock、status 和安全错误摘要的 owner。
- runtime bridge 是唯一 event-loop owner；未来 adapter 不得自行创建 event loop、subprocess 或 HTTP client。
- 以 Phase 1 验证的 stdio SDK API 启动子进程。
- discovery 使用 SDK 公开兼容模式与 tools/list，按其公开分页 API 收集所有 pages。
- 每个发现的 tool 转为 MCPToolDescriptor；本 Phase 不写 adapter 或 registry。
- 启动、协议、tools/list 错误仅更新该 server status，不抛到其他 server。
- 同 server 预留串行 lock，供后续 tools/call 使用。

### 测试范围

- fake stdio MCP server 或 SDK 官方 fixture。
- 成功 discovery、分页、command 无法启动、协议/列表失败。
- 一个 server failure 不影响另一个 stdio server。
- 显式 shutdown 可释放资源。

### 完成标准

- stdio server 只启动一次，discovery 可取得 descriptor，shutdown 正常。
- 未修改 Registry、Mode、AgentLoop、Permission 或 CLI。

### 验收命令

    .\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q

### 风险

同步测试与 async SDK 生命周期、以及 Windows 子进程关闭语义需要真实 fixture 覆盖。

## Phase 4：Streamable HTTP、多 server、缓存和 shutdown

### 目标

扩展 manager 以支持 Streamable HTTP、独立连接 cache、tools/call、故障隔离与幂等 shutdown。

### 涉及文件

修改：

- newcode/mcp/runtime.py
- newcode/mcp/manager.py
- newcode/mcp/types.py
- tests/test_mcp_manager.py

### 核心设计

- 使用 Phase 1 确认的 SDK 公共 HTTP transport/client API；headers 只保存在私有 runtime。
- 每个 server 拥有独立 HTTP client、stdio process、session、lock、状态和错误。
- ready server 的 discovery/call 复用 cache，不重复创建 client 或 subprocess。
- 实现 call_tool_sync(server, remote_name, arguments)，通过 runtime bridge 调用 SDK。
- isError、SDK/transport 异常、不可用状态转为受控 MCP 错误，不成为 ProviderError 或 AgentLoop 崩溃。
- 不实现 automatic reconnect；失效连接只标记 unavailable。
- shutdown 幂等、逐 server 清理；一个关闭失败不能阻断其余 cleanup。

### 测试范围

- fake Streamable HTTP endpoint 或 SDK transport stub，验证 URL/headers 注入但不真实联网。
- 多 server 隔离、同 server cache 重用、tools/call success/isError/exception/closed。
- stdio 与 HTTP 混合运行。
- repeated shutdown。

### 完成标准

- manager 能发现并调用两种 transport 的远端 tool。
- 无 CLI、Registry、Permission 或 AgentLoop 改动。

### 验收命令

    .\.venv\Scripts\python.exe -m pytest tests/test_mcp_manager.py -q

### 风险

HTTP fake 必须覆盖 SDK 公开入口；远端错误路径的 secret 脱敏不可遗漏。

## Phase 5：MCPToolAdapter、Registry 分类与 Mode 可见性

### 目标

将 descriptor 适配为同步 Tool，注册到 ToolRegistry；实现 Do Mode 外部工具可见性，同时保留 Plan Mode 与六个内置工具行为。

### 涉及文件

新增：

- newcode/mcp/adapter.py
- tests/test_mcp_adapter.py

修改：

- newcode/mcp/manager.py
- newcode/tools/registry.py
- newcode/agent/mode.py
- newcode/agent/loop.py
- tests/test_tools_registry.py
- tests/test_agent_modes.py
- tests/test_agent_scheduler.py

### 核心设计

- MCPToolAdapter 实现现有 Tool Protocol：
  - spec 映射内部稳定名、description、parameters；
  - run 调用 manager.call_tool_sync；
  - success/isError/异常映射为现有 ToolResult。
- ToolRegistry 增加显式工具分类与可见性元数据：保留六内置工具原分类；MCP tool 标记为 external、Do-visible、side-effect。
- Do Mode schema 改为 Registry 计算的六内置加 Do-visible 外部工具，不再把固定 DO_TOOL_NAMES 当作完整集合。
- Plan Mode 继续固定三个内置只读工具。
- ToolScheduler 继续依据 Registry 分类，所有 MCP adapter 落入串行 batch。
- 注册前拒绝 schema 无效、内置冲突、稳定名称冲突和 digest collision；失败 tool 跳过，不覆盖已有工具。

### 测试范围

- adapter schema、arguments、success、isError、异常、JSON-safe data 和脱敏 metadata。
- Registry 冲突处理；六内置 schema/分类不变。
- Plan schema 仅三个内置工具；Do schema 包含成功注册 MCP tool。
- MCP tool 不判为只读，mixed calls 的调度/回写顺序稳定。
- Provider 源码和行为不因本 Phase 改动。

### 完成标准

- AgentLoop 能向 Provider 提供 Do Mode MCP schema。
- Plan Mode MCP call 在 mode gate 成为 disallowed_tool，绝不调用 manager。
- Provider 不修改。

### 验收命令

    .\.venv\Scripts\python.exe -m pytest tests/test_mcp_adapter.py tests/test_tools_registry.py tests/test_agent_modes.py tests/test_agent_scheduler.py -q

### 风险

Registry API 改动必须同步 mode/scheduler；adapter 不得把 SDK 原始对象写入 session。

## Phase 6：MCP Permission、规则和 session allow

### 目标

让 MCP tool 完整接入 Permission System，增加 MCP identity match、CLI 安全摘要和所有 mode 下的 conservative built-in policy。

### 涉及文件

修改：

- newcode/permissions/types.py
- newcode/permissions/normalizer.py
- newcode/permissions/rules.py
- newcode/permissions/session.py
- newcode/permissions/builtins.py
- newcode/permissions/confirmer.py
- newcode/permissions/manager.py
- newcode/agent/loop.py

新增/修改测试：

- tests/test_mcp_permissions.py
- tests/test_permissions_rules.py
- tests/test_permissions_manager.py
- tests/test_agent_loop_mcp.py
- tests/test_agent_loop_permissions.py

### 核心设计

- normalizer 从 MCP adapter 生成 mcp_server、mcp_tool、mcp_transport、mcp_arguments。
- PermissionMatch、YAML parser、matcher 支持 exact/glob mcp_server 与 mcp_tool。
- SessionPermissionRules 对 MCP request 创建精确 server/tool session allow rule。
- CLI confirmer 摘要显示安全 MCP identity，不显示 headers/env/secret 或完整 arguments。
- built-in external MCP policy 的固定优先级为：
  1. hard denylist；
  2. workspace sandbox；
  3. session/local/project/user explicit rules；
  4. external MCP built-in policy；
  5. permission mode；
  6. HITL。
- 未命中 explicit allow/deny 的 MCP tool 始终 high-risk require_confirmation，trusted 也不能自动 allow。
- permission deny 复用 permission_denied ToolResult，按原 index 回灌；adapter/manager 不被调用。
- 当前 hard denylist 和 workspace sandbox 继续在各自适用范围保护内置工具；不猜测或声称可 sandbox 任意远端 MCP 参数。

### 测试范围

- MCP explicit allow/deny、server/tool glob。
- 四种 PermissionMode 下未授权 MCP tool 都要求确认。
- once/session confirmation；session rule 只匹配同一 MCP identity。
- deny 不调用 adapter/manager，permission_denied 回灌下一轮。
- command/path rules、sensitive file、hard denylist、workspace sandbox 全部回归。

### 完成标准

- MCP tool 无法绕过 PermissionManager。
- 非 MCP 工具权限行为无回归。
- trusted 模式下未知 MCP tool 不会静默放行。

### 验收命令

    .\.venv\Scripts\python.exe -m pytest tests/test_mcp_permissions.py tests/test_permissions_rules.py tests/test_permissions_manager.py tests/test_agent_loop_permissions.py tests/test_agent_loop_mcp.py -q

### 风险

session allow 必须精确，不能因 slug 相似误放行；不能把 MCP 远端风险错误地映射为本地 sandbox 已覆盖。

## Phase 7：CLI 生命周期、启动发现与端到端 AgentLoop

### 目标

让 CLI 成为 MCP config、discovery、adapter 注册与 finally shutdown 的唯一 owner；真实 AgentLoop 通过既有 registry 链路调用获准 MCP tool。

### 涉及文件

修改：

- newcode/cli.py
- newcode/prompt/modules.py（仅增加稳定的外部工具权限提醒，如确有必要）
- newcode/prompt/reminder.py（仅在既有 permission reminder 需补充时）

新增/修改测试：

- tests/test_cli_mcp.py
- tests/test_cli_agent_loop.py
- tests/test_cli_permissions.py
- tests/test_prompt_builder.py（如 prompt 有改动）

### 核心设计

- main 在创建内置 registry、ToolContext、PermissionManager 后加载独立 MCP config、创建 manager、逐 server discovery 并注册成功 adapter。
- 单 server failure 输出脱敏 warning，CLI 仍可使用内置工具和其他 server。
- startup 后的 run_conversation 必须置于 try/finally；EOF、/exit、KeyboardInterrupt、交互异常均调用幂等 manager.shutdown。
- run_conversation 保持依赖注入可测，不直接管理 transport。
- AgentLoop 只从 registry 执行 MCPToolAdapter，不持有 SDK/transport。
- Prompt 仅可稳定提醒外部工具受权限检查，不注入 server、URL、header、env、状态、错误或 secret。

### 测试范围

- 无 MCP config 时 CLI 行为不变。
- 一个 discovery failure 不影响普通聊天/内置工具。
- Do Mode schema 含 MCP tool，Plan Mode 不含。
- MCP confirmation allow/deny 后工具执行及 tool result 回灌正确。
- EOF、/exit、KeyboardInterrupt、异常均触发 shutdown。
- CLI 不泄露 DSML、secret、headers、env。

### 完成标准

- CLI 是唯一 MCP lifecycle owner。
- 获准 MCP tool 可真实通过 AgentLoop 执行；拒绝时不访问远端。
- Provider、六工具执行和既有 CLI commands 不变。

### 验收命令

    .\.venv\Scripts\python.exe -m pytest tests/test_cli_mcp.py tests/test_cli_agent_loop.py tests/test_cli_permissions.py tests/test_cli_tool_flow.py tests/test_cli.py -q

### 风险

CLI early return/exception 易遗漏 shutdown；warning 需清晰且脱敏。

## Phase 8：全量回归、边界检查和人工验收

### 目标

验证 MCP Client 不扩大职责边界，且 Chapter 4/5/6 和 Function Calling 能力无回归。仅修复测试明确揭示的最小问题。

### 涉及文件

- 原则上不新增生产代码。
- 仅修改失败测试证明存在回归的最小模块。
- 扩展/整理 MCP、Agent、Permission、CLI 回归测试。

### 核心设计

- 静态边界：Provider 不导入 newcode.mcp/permissions/prompt；MCP 模块不导入 Provider；AgentLoop 不导入 SDK transport。
- 不新增 cache_control、legacy SSE、automatic reconnect 或本章外 MCP 功能。
- 验证 MCP 串行、Plan Mode 不可见、未知 MCP 需确认、拒绝会回灌。

### 测试范围

- MCP 全部新增测试。
- Tool Registry、scheduler、AgentLoop、modes、permissions、CLI、Prompt、Provider、Function Calling 回归。
- 人工验收：
  1. 无 MCP config 可正常启动和普通聊天；
  2. 有效 stdio server 被发现；
  3. 无效 server 不影响内置工具；
  4. Do Mode 可调用获准 MCP tool；
  5. Plan Mode MCP tool 被拒绝且不访问远端；
  6. trusted 下未知 MCP tool 仍要求确认；
  7. /exit 后 stdio/HTTP 资源释放。

### 完成标准

- 全量 pytest 与 compileall 通过。
- CLI 人工验收通过。
- 无新增 MCP 以外工具，六内置工具不变。
- Provider、Prompt、Permission、AgentLoop 边界符合 spec。

### 验收命令

    .\.venv\Scripts\python.exe -m compileall newcode
    .\.venv\Scripts\python.exe -m pytest -q --basetemp "$env:TEMP\newcode-pytest-mcp"

## 依赖和顺序

- Phase 1 是阻断前置；所有 SDK 调用必须基于其锁定版本公开 API。
- Phase 2 为 Phase 3/4/5 提供 config、name 与 schema 输入。
- Phase 3 先建立 stdio lifecycle；Phase 4 扩展 HTTP、cache、call 和 cleanup。
- Phase 5 后 MCP tool 才可进入 Do Mode schema；Phase 6 前不得使 CLI 实际暴露可调用 MCP tool。
- Phase 6 先于 Phase 7，确保 CLI 接入不会出现 permission bypass。
- Phase 8 在全部 targeted tests 通过后进行。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| SDK API 不符合预期 | Phase 1 锁版本并以公开 API 兼容测试阻断后续工作 |
| 远端名称/schema 不兼容 Provider | Phase 2 纯函数校验，Phase 5 注册前拒绝 |
| secret 通过异常泄露 | 私有敏感值通道、manager 脱敏、executor 既有遮蔽和专项测试 |
| async SDK 与同步 Tool 冲突 | runtime bridge 是唯一 async owner |
| 多 server 相互影响 | 独立状态、lock、session、client、错误和 cleanup |
| MCP 绕过权限 | Phase 6 在 CLI 之前完成，AgentLoop precheck 是唯一执行入口 |
| trusted 自动放行 | MCP built-in confirmation 位于 permission mode 之前 |
| CLI 漏 shutdown | Phase 7 try/finally 覆盖所有退出路径 |
| 内置能力回归 | 每个扩展阶段运行对应 Registry/Mode/Agent/Permission 回归 |
