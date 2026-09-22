# Chapter 10：Command Registry & Dispatcher 执行清单

> 每项先运行 `python -m compileall newcode`，再以系统 TEMP basetemp 执行 targeted pytest；全量测试不能替代局部验证。

- [ ] **T1 模型**：前置无；允许 `newcode/commands/{__init__,types}.py`、registry 测试。核对类别、元数据、outcome、安全码、大小写规范化。禁止 CLI/Provider/工具依赖。验证：`tests/test_commands_registry.py`。证据：不可变模型/安全错误。
- [ ] **T2 注册**：前置 T1；允许 registry/测试。核对规范名和别名全局冲突失败、可见帮助、排序。禁止动态/配置注册。验证：`tests/test_commands_registry.py`。证据：零覆盖。
- [ ] **T3 解析补全**：前置 T1–T2；允许 dispatcher/测试。核对 case-insensitive、未知 `/help`、普通文本、唯一替换/多菜单/隐藏排除。禁止 handler、AI、工具、文件。验证：`tests/test_commands_dispatcher.py`。证据：无副作用。
- [ ] **T4 UI 边界**：前置 T1；允许 ui/测试。核对 UIControl 和 fake UI，handler 无直接 print。禁止 TUI/GUI/依赖。验证：`tests/test_commands_ui.py`。证据：渲染无关。
- [ ] **T5 本地命令**：前置 T2–T4；允许 builtins/测试和测试证明的最小读取 accessor。核对 help/compact/clear/session/memory/permission/status、`/sessions`/`/resume` 兼容、脱敏、安全根、clear 新会话。禁止主模型、工具、MCP、写记忆。验证：`tests/test_commands_builtins.py tests/test_session_persistence.py tests/test_context_manager.py tests/test_memory_store.py`。
- [ ] **T6 状态/review**：前置 T5；允许 builtins/测试。核对 plan/do 只改 mode；review 无参数、固定 AI input、不直连 Provider。禁止动态 prompt/命令级权限。验证：`tests/test_commands_builtins.py tests/test_agent_modes.py`。
- [ ] **T7 CLI**：前置 T3–T6；允许 `newcode/cli.py`、CLI 命令测试和必要既有 CLI 测试。核对 exit 优先、仅 ai_input 入 AgentLoop、compact/session/EOF/finally 语义。禁止改 Provider、MCP lifecycle、Permission confirmer。验证：`tests/test_cli_commands.py tests/test_cli.py tests/test_cli_session.py tests/test_cli_context.py`。
- [ ] **T8 gate**：前置 T7；允许回归测试，源码仅失败证明时最小修复。核对 review 的 Plan/Do、Permission、ToolScheduler、MCP、Context、Memory gate；隐藏补全零泄露。禁止真实网络/secret/第三方 MCP。验证：`tests/test_cli_commands.py tests/test_agent_loop_permissions.py tests/test_agent_loop_mcp.py tests/test_cli_mcp.py tests/test_cli_memory.py tests/test_agent_loop_context.py`。
- [ ] **T9 全量**：前置 T1–T8；允许仅最小修复。核对 commands 不导入 Provider/MCP client/ToolScheduler；review 不绕过 AgentLoop；本地命令无工具执行；状态无网络探测。验证：`python -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter10-final"`、`git diff --check`。证据：全量结果、skip 原因、fake CLI 验收；不装 tmux。

## Phase 验证

| Phase | 任务 | targeted pytest |
|---|---|---|
| 1 | T1–T4 | `tests/test_commands_registry.py tests/test_commands_dispatcher.py tests/test_commands_ui.py` |
| 2 | T5–T7 | `tests/test_commands_builtins.py tests/test_cli_commands.py tests/test_cli.py tests/test_cli_session.py tests/test_cli_context.py tests/test_memory_store.py` |
| 3 | T8 | `tests/test_cli_commands.py tests/test_agent_loop_permissions.py tests/test_agent_loop_mcp.py tests/test_cli_mcp.py tests/test_cli_memory.py tests/test_agent_loop_context.py` |
| 4 | T9 | full pytest、compileall、diff check、fake CLI fixture |

每 Phase：

```powershell
python -m compileall newcode
python -m pytest <上表 tests> -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter10-phaseN"
```

## 最终证据

- [ ] 十个内置命令、兼容别名、帮助、补全可见性和排序通过。
- [ ] 冲突、未知、参数错误、隐藏、敏感值、跨 workspace、cleanup failure 安全。
- [ ] review 经 AgentLoop，Plan/Do、Permission、MCP、Context、Memory、调度未绕过。
- [ ] 无网络、RAG、向量库、同步、自定义命令或动态 prompt。
- [ ] 全量结果和 skip 原因已记录；未使用真实网络、生产 secret、第三方 MCP 或 tmux。
