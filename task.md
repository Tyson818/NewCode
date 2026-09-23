# Chapter 11：Skill System 原子任务

## T1：Skill 模型与错误码

前置：无。允许：`newcode/skills/__init__.py`、`types.py`、`tests/test_skills_types.py`。动作：定义 frontmatter、metadata、mode、参数、activation、资源摘要、稳定错误；isolated `history_messages` 仅允许 0–20。测试：schema/name/mode、history 的 0/20/越界、model/参数边界。完成：纯数据模型。边界：不读文件、不改 Prompt/Agent。

## T2：路径安全与三级发现

前置：T1。允许：`discovery.py`、`tests/test_skills_discovery.py`、`tests/test_skills_paths.py`。动作：三根、单文件/目录入口、project 覆盖、坏项隔离、digest、root/resource sandbox，以及模型启动目录投影。测试：symlink、绝对/`..`、同名、坏 YAML、普通文件限制；目录注入仅 name/description，tools/mode/model/parameters/path/digest/SOP 零泄露。完成：只返回内部 metadata 与最小模型目录。边界：不加载 SOP 正文到 Prompt。

## T3：内置模板资源

前置：T1。允许：`newcode/resources/skills/{commit,review,test}/SKILL.md`、discovery 测试。动作：提供固定模板与白名单：commit/test 可声明 `run_command`，review 维持只读。测试：发现、描述、白名单、commit 不自动提交；commit/test 的 `run_command` 在 Permission deny 或需确认时零绕过。完成：无自动脚本执行。边界：不新增依赖或网络。

## T4：按需 loader 与参数

前置：T1–T2。允许：`loader.py`、`tests/test_skills_loader.py`。动作：完整 SOP、参数替换、资源索引、模型/工具校验。测试：未声明/缺失/额外参数、secret 脱敏、未知工具、指定模型失败。完成：失败零激活。边界：不调用 Provider/工具。

## T5：激活状态与热更新

前置：T2、T4。允许：`state.py`、`tests/test_skills_state.py`。动作：多激活排序、stale/invalid、clear/new/resume 空状态、白名单交集。测试：变更/删除/损坏、顺序无关、load_skill 例外。完成：不持久化到 session。边界：不改 Context 算法。

## T6：Prompt 动态注入

前置：T5。允许：`newcode/prompt/modules.py`、`builder.py`、`tests/test_prompt_skills.py`，必要 prompt 测试。动作：active SOP 在动态背景首项，每轮生成，带来源/非授权标签。测试：顺序、多个 Skill、session/JSONL 零写入、stale 排除。完成：完整 SOP 不在启动 prompt。边界：不改稳定 Prompt。

## T7：系统 load_skill 与工具策略

前置：T4–T6。允许：`tool.py`、`policy.py`、`newcode/agent/loop.py`、必要 `tools/registry.py`、测试。动作：注册 load_skill、接入 AgentLoop 可见工具过滤与刷新。测试：load_skill 在 Plan/Do 均可见可调用、写 activation state 始终串行且不进入只读并发批、完整 Permission→Scheduler→executor 链路、MCP、unknown whitelist、普通工具交集与系统例外。完成：Provider 不导入 Skill。边界：不直调 executor/MCP client。

## T8：short command overlay

前置：T2、T5、T7。允许：`commands.py`、必要 `newcode/commands/*`、`tests/test_skills_commands.py`。动作：成功 activation 后的不可变 overlay、`key=value` 参数、冲突、隐藏、help、completion、热更新。测试：发现不注册、load 成功注册、clear/resume/无效移除、不改 sealed registry；静态 `/review` 固定行为优先，review Skill 可加载但不创建 overlay，其他无冲突 Skill 仍创建短命令。完成：无任意命令注册。边界：不改十个内置命令语义。

## T9：shared 执行

前置：T6–T8。允许：`runner.py`、`newcode/cli.py`、`tests/test_skills_runner.py`、`tests/test_cli_skills.py`。动作：short command/shared skill 经正常 AgentLoop，加载/激活并保持历史。测试：固定参数、工具 gate、Context/Memory/session 回归。完成：无直接 Provider 调用。边界：不绕过 Permission/Plan-Do。

## T10：isolated 执行与回流

前置：T9。允许：runner、CLI、相关测试。动作：有限历史 child session、同 gate、模型能力、摘要回流、finally cleanup。测试：`history_messages=0` 不携带历史、20 上限、只携带最近已脱敏 user/assistant 非工具消息、工具调用/结果零携带、主/子 session/memory/artifact/active Skills 零共享、敏感值不回流、失败隔离。完成：child 原文零持久化。边界：不创建远端会话/网络。

## T11：CLI 生命周期与回归

前置：T8–T10。允许：`newcode/cli.py`、`tests/test_cli_skills.py`及失败直接证明的最小测试。动作：启动 catalog、每回合刷新、clear/new/resume 清激活、finally 关闭隔离。测试：EOF、exit、异常、MCP/Memory/Context cleanup 顺序。完成：既有命令不回归。边界：不改 Provider 配置。

## T12：全量验收

前置：T1–T11。允许：仅失败直接证明的最小模块与测试。动作：全量验证、静态审计、fake CLI 验收。完成：记录 skip 原因。边界：不进入 Chapter 12。

每任务均执行：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest <该任务 targeted tests> -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter11-tN"
```

出现路径逃逸、启动目录 metadata/SOP 泄露、gate 绕过、静态 `/review` 被 overlay 覆盖、模型静默回退或范围外设计需求时立即停止。
