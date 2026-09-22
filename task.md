# Chapter 9：Project Instructions、Session Persistence 与 Automatic Memory 任务

## 文件清单

预计新增：`newcode/instructions.py`、`newcode/persistence.py`、`newcode/memory/__init__.py`、`newcode/memory/types.py`、`newcode/memory/store.py`、`newcode/memory/service.py`，以及 `tests/test_instructions.py`、`tests/test_session_persistence.py`、`tests/test_memory_store.py`、`tests/test_memory_service.py`、`tests/test_prompt_memory.py`、`tests/test_agent_loop_memory.py`、`tests/test_cli_session.py`、`tests/test_cli_memory.py`。

预计修改：`newcode/session.py`、`newcode/prompt/modules.py`、`newcode/prompt/builder.py`、`newcode/agent/loop.py`、`newcode/cli.py`，以及必要的既有对应测试。不得修改 Provider、MCP、Permission、ToolRegistry、ToolScheduler、依赖或现有 Context Management 的安全语义。

## T1：路径与错误基础

前置：无。允许：`newcode/instructions.py`、`tests/test_instructions.py`。动作：定义受控 user/workspace 根、相对路径验证、符号链接/逃逸拒绝与安全错误码。测试：恶意绝对路径、`..`、link、编码/读取失败。完成：根外零读写且错误不泄露。边界：不加载 prompt、不写 session。

## T2：三层指令和 include

前置：T1。允许：`newcode/instructions.py`、`tests/test_instructions.py`。动作：加载 user/workspace/project，低至高合并，5 层 include、visited 防环、来源标记。测试：优先级、环、重复、超深、单层失败隔离。完成：结果稳定且无 session 变更。边界：不让指令授权工具。

## T3：会话 ID 与 JSONL 序列化

前置：T1。允许：`newcode/session.py`、`newcode/persistence.py`、`tests/test_session_persistence.py`、必要的 `tests/test_session.py`。动作：生成并校验 `YYYYMMDD-HHMMSS-[a-z0-9]{4}` ID，后缀使用安全随机源；同秒文件冲突仅重抽后缀、最多 16 次，排他创建且绝不覆盖已有会话。版本 header、脱敏 JSON-safe message/checkpoint。测试：格式、字符集、同秒不冲突、16 次耗尽安全失败与零覆盖、稳定 round-trip、敏感遮蔽、原子失败回退。完成：不会持久化 raw secret 或覆盖归档。边界：不接 CLI。

## T4：恢复、截断与 30 天清理

前置：T3。允许：`newcode/persistence.py`、`newcode/session.py`、`tests/test_session_persistence.py`。动作：坏行跳过、有效记录恢复、未配对尾部截断、24 小时提醒信息、30 天限定清理。测试：损坏 JSONL、孤立 result、未完成 call、活动/根外/链接保留。完成：Provider 前消息合法。边界：不删除 sandbox 外或坏文件。

## T5：记忆类型、frontmatter 与 store

前置：T1。允许：`newcode/memory/__init__.py`、`types.py`、`store.py`、`tests/test_memory_store.py`。动作：用户偏好、纠正反馈、项目知识、参考资料四类、user/project scope、frontmatter、索引和原子写。测试：workspace 指纹隔离、字段校验、敏感遮蔽、半成品失败，以及两个 scope 各自 200 行和 25 KB 的边界/超限拒绝。完成：两个限制均满足且不跨 scope。边界：不调用 LLM。

## T6：受限记忆选择与动态提示

前置：T5、T2。允许：`newcode/memory/store.py`、`newcode/prompt/modules.py`、`newcode/prompt/builder.py`、`tests/test_memory_store.py`、`tests/test_prompt_memory.py`。动作：8 条/6000 字符稳定选择，组合指令与记忆为动态 system message，并使 project、workspace、user 指令按高到低出现。测试：三层优先级/注入顺序、空/失败回退、不会写入 session。完成：来源与非授权提示可观察。边界：不改 reminder/Provider。

## T7：记忆 LLM 结果与去重

前置：T5。允许：`newcode/memory/service.py`、`store.py`、`tests/test_memory_service.py`。动作：构造零工具、零文件读取的受限去重请求，严格解析 create/update/merge/ignore。测试：重复合并、格式错、远端异常、敏感输入。完成：LLM 决策而非规则去重，失败零写入。边界：单次任务最多一次请求，不递归。

## T8：异步服务与关闭

前置：T7。允许：`newcode/memory/service.py`、`tests/test_memory_service.py`。动作：单 worker/有界队列、幂等 shutdown、有界等待与失败隔离。测试：提交不阻塞、队列满、安全取消/关闭、写入顺序。完成：无任务阻断主线程。边界：不引入网络或工具。

## T9：AgentLoop 自然完成集成

前置：T6、T8。允许：`newcode/agent/loop.py`、必要时 `newcode/agent/events.py`、`tests/test_agent_loop_memory.py`、必要时 `tests/test_agent_loop.py`。动作：请求前读动态背景；仅 `AgentFinalAnswer` 后提交快照。测试：final 一次、provider error/cancel/permission deny/max iteration 零提交、工具顺序不变。完成：主回复无等待。边界：不绕过 Plan/Do、Permission、MCP、Scheduler。

## T10：CLI session 生命周期

前置：T4、T9。允许：`newcode/cli.py`、`persistence.py`、`tests/test_cli_session.py`。动作：`/sessions` 仅展示当前 workspace 可恢复会话的 ID、标题/安全摘要、最后更新时间、消息数；`/resume <session-id>` 恢复指定会话，配合 checkpoint、24h 提醒、30d 清理。测试：无归档行为保持、恢复、坏文件、无效/过期/跨 workspace/不可恢复 ID 的脱敏提示。完成：CLI 输出与列表均脱敏、无跨 workspace 信息泄露。边界：不改配置/Provider。

## T11：CLI memory/cleanup 编排

前置：T8、T10。允许：`newcode/cli.py`、`tests/test_cli_memory.py`、必要时 `tests/test_cli_context.py`。动作：finally 依序安全归档、memory shutdown、context cleanup，互不阻断。测试：EOF、`/exit`、KeyboardInterrupt、loop 异常、启动后异常、MCP cleanup 回归。完成：每条路径可观察。边界：不清除其他 session/workspace。

## T12：Chapter 9 targeted 回归

前置：T1–T11。允许：仅失败证明所需最小模块与测试。动作：运行所有 Chapter 9 测试及 Prompt/Session/AgentLoop/CLI/Context 回归。测试：`python -m compileall newcode`；`python -m pytest tests/test_instructions.py tests/test_session_persistence.py tests/test_memory_store.py tests/test_memory_service.py tests/test_prompt_memory.py tests/test_agent_loop_memory.py tests/test_cli_session.py tests/test_cli_memory.py tests/test_context_*.py -q --basetemp "$env:TEMP\newcode-pytest-chapter9-targeted"`。完成：全绿。边界：不以范围外重构解决。

## T13：全量验收

前置：T12。允许：同 T12。动作：静态依赖审计、fake CLI 人工验收。测试：`python -m compileall newcode`；`python -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter9-final"`；`git diff --check`。完成：记录 skip 原因、无真实网络/secret。边界：不安装 tmux；无法交互时以自动化 fixture 证据替代。

## 执行顺序

`T1 → T2 → T3 → T4`；`T1 → T5 → T6`；`T5 → T7 → T8`；`T6 + T8 → T9 → T10 → T11 → T12 → T13`。
