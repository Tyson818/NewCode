# Chapter 9：Project Instructions、Session Persistence 与 Automatic Memory 验收清单

> 每项在实现后以本地 fake provider、临时 home/workspace 和系统 TEMP basetemp 验证；不使用真实网络、生产 secret 或第三方 MCP。

## Phase 1：指令与路径安全

- [ ] T1 路径安全：绝对路径、`..`、符号链接、workspace/user-root 外路径均被拒绝且诊断脱敏。允许：`newcode/instructions.py`、`tests/test_instructions.py`。禁止：任何 prompt/CLI 接入。验证：`python -m compileall newcode`；`python -m pytest tests/test_instructions.py -q --basetemp "$env:TEMP\newcode-pytest-chapter9-p1"`。证据：根外零读写。
- [ ] T2 三层指令：加载顺序 user < workspace < project，动态注入顺序 project → workspace → user；include 最深 5 层、visited 防环/重复、单层失败隔离。允许同上。禁止：把指令写入 session 或作为工具授权。验证同 T1。证据：来源/优先级与注入顺序稳定。

## Phase 2：归档与恢复

- [ ] T3 JSONL：`YYYYMMDD-HHMMSS-[a-z0-9]{4}` session ID，后缀由安全随机源生成；同秒冲突仅重抽后缀、最多 16 次，耗尽后安全失败且零覆盖。版本 header、JSON-safe 脱敏 checkpoint/原子写。允许：`newcode/session.py`、`newcode/persistence.py`、`tests/test_session_persistence.py`、必要 `tests/test_session.py`。禁止：workspace 外写入或覆盖已有归档。验证：`python -m compileall newcode`；`python -m pytest tests/test_session.py tests/test_session_persistence.py -q --basetemp "$env:TEMP\newcode-pytest-chapter9-p2"`。证据：格式、字符集、冲突耗尽、round-trip 与 secret 遮蔽。
- [ ] T4 恢复/清理：坏行跳过，未配对 tool-call/result 尾部截断，24h 提醒，30 天仅 session 根内清理。允许同 T3。禁止：删除活动/链接/坏文件或根外内容。验证同 T3。证据：可发送的恢复历史和范围测试。

## Phase 3：记忆存储与注入

- [ ] T5 笔记 store：用户偏好、纠正反馈、项目知识、参考资料四类别，user/project 隔离、frontmatter、每个 scope 最多 200 行且最大 25 KB 的双重索引上限、原子写。允许：`newcode/memory/**`、`tests/test_memory_store.py`。禁止：LLM、工具、跨项目注入。验证：`python -m compileall newcode`；`python -m pytest tests/test_memory_store.py -q --basetemp "$env:TEMP\newcode-pytest-chapter9-p3"`。证据：四类别、frontmatter、隔离、两个独立上限和脱敏。
- [ ] T6 请求前背景：最多 8 条/6000 字符，dynamic system message 位于 stable 后、reminder 前，绝不改写 session 用户原文。允许：memory store、`newcode/prompt/modules.py`、`builder.py`、相关测试。禁止：Provider 改动。验证：`python -m pytest tests/test_memory_store.py tests/test_prompt_memory.py tests/test_prompt_builder.py -q --basetemp "$env:TEMP\newcode-pytest-chapter9-p3"`。证据：顺序与无会话污染。

## Phase 4：异步自动记忆

- [ ] T7 LLM 去重：请求零工具、零文件读取、单任务最多一次；仅严格 create/update/merge/ignore 可写。允许：`newcode/memory/service.py`、`store.py`、`tests/test_memory_service.py`。禁止：递归、真实网络、原始响应落盘。验证：`python -m compileall newcode`；`python -m pytest tests/test_memory_service.py -q --basetemp "$env:TEMP\newcode-pytest-chapter9-p4"`。证据：格式失败零写入。
- [ ] T8 worker/shutdown：有界异步、提交不阻塞、幂等关闭和失败隔离。允许同 T7。禁止：阻断 CLI/Context/MCP 清理。验证同 T7。证据：取消、队列、原子文件测试。
- [ ] T9 Loop 集成：仅自然 `AgentFinalAnswer` 后异步提交；错误、取消、deny、Plan Mode、max iteration 均零提交；原始工具顺序及 gate 不变。允许：`newcode/agent/loop.py`、必要 events/tests。禁止：绕过 Provider、MCP、Permission、ToolScheduler。验证：`python -m compileall newcode`；`python -m pytest tests/test_agent_loop_memory.py tests/test_agent_loop.py tests/test_agent_loop_context.py -q --basetemp "$env:TEMP\newcode-pytest-chapter9-p4"`。证据：主回复先可见。

## Phase 5：CLI 生命周期

- [ ] T10 session CLI：`/sessions` 只展示当前 workspace 可恢复会话的 ID、标题/安全摘要、最后更新时间、消息数；`/resume <session-id>` 恢复指定会话。无效、过期、跨 workspace、不可恢复 ID 与坏文件均安全提示；checkpoint、24h 提醒和 30d 清理安全。允许：`newcode/cli.py`、persistence、`tests/test_cli_session.py`。禁止：改变 `/plan`、`/do`、`/compact`、Provider 配置，或泄露其他 workspace 会话。验证：`python -m compileall newcode`；`python -m pytest tests/test_cli_session.py tests/test_cli_context.py -q --basetemp "$env:TEMP\newcode-pytest-chapter9-p5"`。证据：无 session 配置时旧行为不变，列表和错误均脱敏。
- [ ] T11 finally：EOF、`/exit`、KeyboardInterrupt、AgentLoop/启动后异常均按归档→memory shutdown→context cleanup 处理；单项失败不阻断 MCP shutdown。允许：CLI 与上述 tests。禁止：清理 sandbox 外、其他 session、工作区文件。验证：`python -m pytest tests/test_cli_session.py tests/test_cli_memory.py tests/test_cli_context.py tests/test_cli_mcp.py -q --basetemp "$env:TEMP\newcode-pytest-chapter9-p5"`。证据：路径和敏感输出安全。

## Phase 6：最终验收

- [ ] T12 targeted：Chapter 9、Prompt、Session、AgentLoop、CLI、Context 回归全部通过。允许：仅直接失败的最小修复/测试。禁止：范围外重构。验证：`python -m compileall newcode`；`python -m pytest tests/test_instructions.py tests/test_session_persistence.py tests/test_memory_store.py tests/test_memory_service.py tests/test_prompt_memory.py tests/test_agent_loop_memory.py tests/test_cli_session.py tests/test_cli_memory.py -q --basetemp "$env:TEMP\newcode-pytest-chapter9-targeted"`。证据：通过数。
- [ ] T13 全量与人工：`python -m compileall newcode`、`python -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter9-final"`、`git diff --check` 均通过；静态确认 Provider 不导入本章模块、Context/Memory 不调用工具或权限执行层。以 fake CLI 覆盖指令→恢复→回答→后台记忆→退出；记录 skip 原因。禁止：tmux 安装、真实网络/secret。证据：命令结果；若环境无 tmux，说明以自动化 fixture 代替。
