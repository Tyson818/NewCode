# Chapter 11：Skill System 执行清单

> 每项先执行 `.venv\Scripts\python.exe -m compileall newcode`，再使用系统 TEMP basetemp 运行所列 targeted pytest；全量验证不能替代局部验证。

- [ ] **T1 模型**：前置无；允许 `newcode/skills/{__init__,types}.py`、类型测试。核对 name/description/tools/mode/history/model/parameters、稳定错误，isolated history 仅 0–20。禁止读文件/Prompt/Agent。验证：`tests/test_skills_types.py`。
- [ ] **T2 发现与 sandbox**：前置 T1；允许 discovery、发现/路径测试。核对 project>user>builtin、单文件/目录、坏项隔离、digest、普通文件、symlink/绝对/`..` 拒绝；模型启动目录逐项仅 name/description，tools/mode/model/parameters/path/digest/SOP 零泄露。禁止 SOP 全量加载。验证：`tests/test_skills_discovery.py tests/test_skills_paths.py`。
- [ ] **T3 内置模板**：前置 T1；允许三份内置 `SKILL.md`、发现测试。核对 commit/test 可声明 `run_command`、review 只读；commit 不自动提交；commit/test 的命令调用在 Permission deny 或确认时零绕过。禁止自动脚本/网络。验证：发现及 Permission 回归测试。
- [ ] **T4 loader/参数**：前置 T1–T2；允许 loader/测试。核对按需 SOP、一次纯文本替换、资源索引、模型与工具校验。禁止 Provider/工具调用。验证：`tests/test_skills_loader.py`。
- [ ] **T5 state/热更新**：前置 T2、T4；允许 state/测试。核对激活排序、交集、stale、invalid、clear/new/resume 清空。禁止 session/JSONL/Memory 持久化。验证：`tests/test_skills_state.py`。
- [ ] **T6 Prompt**：前置 T5；允许 prompt modules/builder/测试。核对 active SOP 为第一动态背景、来源/非授权标记、每轮钉入、零 session 写入。禁止改稳定 prompt/启动全量 SOP。验证：`tests/test_prompt_skills.py tests/test_prompt_memory.py`。
- [ ] **T7 load_skill/gate**：前置 T4–T6；允许 tool/policy/loop、必要 registry、测试。核对 `load_skill` 在 Plan/Do 均可见可调用，不受 whitelist 交集限制，但完整经过 AgentLoop→Permission→串行 Scheduler→executor，且 activation 写操作绝不进入只读并发批；普通工具仍取 Plan/Do 与 whitelist 交集，覆盖 MCP/unknown 工具。禁止 Provider 依赖或 executor 直调。验证：`tests/test_skills_tool.py tests/test_agent_loop_skills.py tests/test_agent_loop_permissions.py tests/test_agent_loop_mcp.py`。
- [ ] **T8 overlay**：前置 T2、T5、T7；允许 commands 与必要 Chapter 10 文件/测试。核对成功 load 后才注册短命令、`key=value` 参数、冲突、help/completion、隐藏、热更新失效、clear/resume 移除，不改 sealed registry；静态 `/review` 固定行为优先，review Skill 可加载但零 overlay，其他无冲突 Skill 可注册。禁止任意自定义命令。验证：`tests/test_skills_commands.py tests/test_commands_registry.py tests/test_commands_dispatcher.py`。
- [ ] **T9 shared**：前置 T6–T8；允许 runner/CLI/测试。核对短命令/shared 经 AgentLoop、Context、Memory、session 和工具 gate。禁止直接 Provider。验证：`tests/test_skills_runner.py tests/test_cli_skills.py tests/test_agent_loop_context.py tests/test_agent_loop_memory.py`。
- [ ] **T10 isolated**：前置 T9；允许 runner/CLI/测试。核对 history=0 零历史、20 上限、最近已脱敏 user/assistant 非工具消息、tool call/result 零携带、主/子 session/memory/artifact/active Skills 零共享、同 gate、安全摘要回流、artifact finally 清理。禁止 child 原文/工具输出持久化、远端会话。验证：runner/CLI/Context/Permission/MCP 测试。
- [ ] **T11 生命周期**：前置 T8–T10；允许 CLI/测试和失败证明的最小文件。核对刷新、clear/new/resume、EOF/exit/异常、cleanup 隔离。禁止改 Provider 配置或旧命令语义。验证：`tests/test_cli_skills.py tests/test_cli_session.py tests/test_cli_context.py tests/test_cli_memory.py tests/test_cli_mcp.py`。
- [ ] **T12 验收**：前置 T1–T11；仅最小修复。核对静态 import、路径、敏感值、无网络/RAG/同步/动态执行；记录 skip。验证：全量 pytest、compileall、diff check、fake CLI。

## Phase 验证

| Phase | 任务 | targeted pytest |
|---|---|---|
| 1 | T1–T3 | `test_skills_types.py test_skills_discovery.py test_skills_paths.py` |
| 2 | T4–T6 | `test_skills_loader.py test_skills_state.py test_prompt_skills.py test_prompt_memory.py` |
| 3 | T7–T8 | `test_skills_tool.py test_agent_loop_skills.py test_skills_commands.py test_commands_registry.py test_commands_dispatcher.py test_agent_loop_permissions.py test_agent_loop_mcp.py` |
| 4 | T9–T11 | `test_skills_runner.py test_cli_skills.py test_cli_session.py test_cli_context.py test_cli_memory.py test_cli_mcp.py` |
| 5 | T12 | 全量 pytest、compileall、diff check、fake CLI fixture |

每个 Phase：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest <上表 tests> -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter11-phaseN"
```

## 最终验收证据

- [ ] 三级发现、覆盖、坏项隔离、frontmatter、参数、资源 sandbox、内置模板均有可重复证据。
- [ ] 模型启动目录仅 name/description；完整 SOP 仅成功按需加载，active SOP 每轮在最前动态注入且不写 session/JSONL/Memory/artifact。
- [ ] `load_skill` 在 Plan/Do 均可见、串行且完整经过 AgentLoop、Permission、ToolScheduler、executor；shared、isolated 均保持 MCP、Context 和会话安全，普通工具多 Skill 白名单交集稳定。
- [ ] 短命令不覆盖静态命令；静态 `/review` 固定行为优先、review Skill 可加载但零 overlay；帮助/补全不泄露隐藏或敏感信息、clear/new/resume 清激活。
- [ ] isolated 覆盖 history=0、20 边界与仅非工具消息；commit/test 的 `run_command` 在 deny/confirmation 时保持零绕过。
- [ ] 指定模型失败无静默回退；无网络、市场、同步、RAG、向量库、动态脚本或命令级权限绕过。
- [ ] 全量结果、所有 skip 原因与 fake CLI 验收已记录；未使用 tmux、真实网络、生产 secret 或第三方 MCP。
