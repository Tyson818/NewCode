# Chapter 11：Skill System —— 让 NewCode 可复用 AI 操作

## 背景与目标

NewCode 已有请求级动态背景、Context Management、Plan/Do、Permission、ToolScheduler、MCP、会话归档、自动记忆及封存的命令注册中心。本章引入本地、受控、可复用的 Skill：Skill 用 Markdown SOP 表达重复 AI 操作，在需要时才加载完整内容，并且绝不成为绕过既有工具与安全边界的第二执行通道。

目标是发现可信本地 Skill、按需激活、收窄工具可见性、支持 shared 与 isolated 两种受控执行，并提供内置 commit、review、test 模板。Skill 不等于任意脚本、远端插件或命令扩展平台。

## 功能需求

### F1：Skill 格式、参数与资源包

单文件 Skill 是 YAML frontmatter 加 Markdown SOP 正文。frontmatter 必填：唯一 `name`、一句 `description`、`tools` 白名单、`mode`（`shared` 或 `isolated`）；`isolated` 必填整数 `history_messages`，范围为 `0–20`；可选 `model` 与 `parameters`。名称规范化使用 `casefold()`，只允许 `[a-z][a-z0-9-]{0,63}`。`description` 不得承载 SOP、secret 或路径凭据。

正文可使用已声明参数的 `{{parameter_name}}` 占位符。替换是纯文本、一次性、无 shell/模板表达式/文件包含/代码执行；未声明、缺失、重复或额外参数安全失败。短命令参数固定为以 Unicode 空白分隔的 `key=value`，不支持引号、环境变量或 shell 求值；`load_skill` 使用 JSON object 参数。参数值作为用户提供内容明确标记，不自动获得工具权限。

目录型 Skill 的入口固定为 Skill 根内 `SKILL.md`；根内可包含模板、示例、辅助脚本和参考文档。辅助脚本仅作为索引资源，绝不因加载或激活自动执行。所有入口和资源必须为普通文件，解析后的真实路径必须位于该 Skill 根内；绝对路径、`..`、符号链接逃逸、根外资源和不安全文件均拒绝。资源索引只包含安全相对路径、类型与大小摘要，不默认读取正文。

### F2：三级发现、覆盖与容错

发现根按优先级为：项目 `<workspace>/.newcode/skills/`、用户 `~/.newcode/skills/`、内置 `newcode/resources/skills/`。每根只识别直接子项中的 `*.md` 单文件及 `<directory>/SKILL.md` 目录包；不递归把模板或参考文档误作独立 Skill。

相同规范名的有效 Skill 以项目覆盖用户、用户覆盖内置。低优先级版本保留为安全诊断的来源类别，不在帮助、短命令或模型发现表中重复出现。单项读取、frontmatter、schema、路径、模型、资源或工具白名单校验失败时跳过该项，记录不包含正文、secret、绝对路径或完整异常的稳定诊断，不阻断其他 Skill。

发现内部可保留 `SkillMetadata` 以供本地校验和后续按需加载；但启动及刷新时注入模型的 Skill 目录严格只包含每个有效 Skill 的 `name` 与一句 `description`。目录不得包含 tools、mode、model、参数定义、文件路径、digest、完整 SOP、资源正文或参数值；`load_skill` 本身的工具 schema 可按既有 ToolRegistry 正常提供。

### F3：两阶段加载、激活与热更新

`load_skill` 是系统级内置控制工具。它在 Plan Mode 与 Do Mode 均可见且可调用，接受已发现 Skill 名称及参数，在需要时读取并校验完整 SOP、做参数替换、生成安全资源索引，并把成功版本激活到当前会话。它不受已激活 Skill 的工具白名单限制，但仍完整经过 ToolRegistry、AgentLoop、PermissionManager、ToolScheduler 与 executor，以及既有 MCP 与错误脱敏边界；不得直接调用 loader 或绕过工具执行链。它会改变 activation state，因此一律串行调度，不得作为只读工具加入并发批。

激活后的完整 SOP、参数替换结果、来源类别、模式、白名单及 digest 在每轮请求的动态环境上下文中置于所有普通动态背景之前，明确标为“受控 Skill 指令，不授予权限”。它不写入 ChatSession、JSONL、Memory 或 Context artifact。可同时激活多个 Skill，按激活先后稳定排序。

每个主回合前进行轻量发现刷新：新增有效 Skill 可被 `load_skill` 发现；有效内容变更使现有激活标记为 stale，但当前激活快照继续用于当前会话，只有再次 `load_skill` 才替换为新版本；已删除、变为无效或不再通过安全校验的激活立即失效并从动态背景、白名单与短命令状态移除。`/clear`、新 ChatSession 及 `/resume` 均以空激活集开始；激活状态不写入持久会话。

### F4：两种执行模式与模型选择

**shared** Skill 在当前 ChatSession/AgentLoop 内运行。它将已激活 SOP 放入请求背景，用户通过该 Skill 的受控短命令或模型 `load_skill` 后继续正常 AgentLoop；工具结果和最终回答留在主会话。

**isolated** Skill 创建独立受控 ChatSession 与 ContextManager，`history_messages` 只能为 `0–20`：`0` 表示不携带任何主会话历史；大于 `0` 时，仅按最近顺序携带最多 N 条已脱敏的 user/assistant 非工具消息。工具调用与工具结果一律不携带。子会话不共享主会话的 session、Memory、artifact 或已激活 Skill 集；其激活状态仅含本次 isolated Skill 的独立快照。它仍使用相同 workspace、ToolRegistry、PermissionManager、Plan/Do、ToolScheduler、MCP 生命周期和 Context 策略。完成或失败后清理子 Context artifact；只将脱敏后最多 4,000 字符、标明来源的安全摘要回流主会话，不额外发起第二次摘要 LLM 请求。绝不回流子会话原文、完整工具输出、headers、env、URL credential、完整异常或内部链路。

可选 `model` 只能引用当前 Provider 公开声明且应用配置允许的模型名；它不能直接构造 Provider、URL、key 或网络客户端。若指定模型不可用、切换失败或 Provider 不支持受控切换，整个 Skill 加载/执行以 `skill_model_unavailable` 安全失败，**不静默回退**；省略 `model` 时继续使用当前主模型。

### F5：工具白名单与组合

加载时，frontmatter 白名单中的每一项必须存在于启动后最终 ToolRegistry（含已验证 MCP adapter）；任何不存在项使该 Skill 以 `skill_tool_unknown` 失败。激活 Skill 后，主/子 AgentLoop 可见且可执行的普通工具集合为：当前 Plan/Do 可见集合与所有激活 Skill 白名单的**交集**。没有激活 Skill 时保持原有工具集合。空交集是有效的最小权限状态。

`load_skill` 是唯一系统例外：在 Plan/Do 两种模式始终可见，不被 Skill 白名单交集移除，且作为 activation-state 写操作始终串行；它仍走 Permission、ToolScheduler 与 executor。除该例外外，加载后的实际可见/可执行普通工具严格为当前 Plan/Do 工具集合与所有已激活 whitelist 的交集。多个 Skill 按激活顺序排列指令，白名单按集合交集组合；顺序不改变结果。硬 denylist、workspace sandbox、explicit deny、Permission confirmation、Plan Mode、ToolScheduler 串行规则及 MCP 的既有限制优先且不可绕过。

### F6：短命令、内置模板与 Chapter 10 集成

每个 **成功加载并激活** 的无冲突 Skill 自动获得会话级受控短命令 `/<normalized-skill-name>`；发现本身不注册 slash 命令。该命令不是用户任意注册 API，没有别名、不能携带 handler 或权限规则，且只接受已声明的 `key=value` 参数后交给既有 AgentLoop。Chapter 10 封存静态命令始终优先：静态 `/review` 保持原有固定行为，内置同名 `review` Skill 仍可通过 `load_skill` 加载，但绝不创建或覆盖 `/review` Skill overlay，记录 `skill_command_conflict`。与其他静态/退出/已激活 Skill 名称冲突时同样不注册短命令；其他无冲突 Skill 仍按上述语法生成短命令。

短命令在复合命令视图中遵守帮助、隐藏和 Tab 补全规则；activation 变化生成新的不可变 overlay，不修改 Chapter 10 封存静态注册表。失效 Skill 的短命令、帮助和补全候选立即移除；热更新后的短命令保持当前 snapshot，须重新 `load_skill` 才替换为新 SOP。`/clear`、新会话与 resume 移除激活及其短命令。

内置提供三个受控模板：`commit`（`read_file`、`find_files`、`search_code`、`run_command`）与 `test`（`read_file`、`find_files`、`search_code`、`run_command`）可使用命令工具，因而不是只读模板；`review`（`read_file`、`find_files`、`search_code`）维持只读 whitelist。所有模板的每次工具调用仍须经过既有 Plan/Do、Permission、workspace sandbox、ToolScheduler 与 MCP 边界；“commit”模板绝不自动提交。

### F7：安全、错误与关闭

对外错误仅使用稳定码和安全摘要，包括 `skill_discovery_failed`、`skill_frontmatter_invalid`、`skill_name_invalid`、`skill_resource_unsafe`、`skill_tool_unknown`、`skill_parameter_invalid`、`skill_parameter_missing`、`skill_not_found`、`skill_load_failed`、`skill_command_conflict`、`skill_model_unavailable`、`skill_execution_failed`、`skill_isolated_failed`。诊断仅含来源级别、规范名和码；不得泄露 SOP、参数、资源内容、secret、headers、env、URL credential、绝对路径或 stack trace。

Skill 刷新、加载、激活、isolated 子会话与清理失败必须彼此隔离，不能阻断 CLI 的既有 checkpoint、Memory shutdown、Context artifact cleanup 与 MCP shutdown。禁止真实网络、远端下载、市场分发、版本管理、团队同步、跨设备同步、动态脚本执行、命令级权限绕过、RAG 或向量库。

## 非功能要求

- 使用 fake provider、临时 workspace/user-home、临时内置资源、fake MCP/tool/permission/UI fixture 测试；不使用生产 secret、真实第三方 MCP 或网络。
- 文件/资源大小、索引条数和隔离历史均有固定上限：单入口 SOP 最大 64 KiB、每包安全资源索引最多 200 项/1 MiB 元数据、isolated `history_messages` 为 0–20。
- 每 Phase 必跑 `python -m compileall newcode` 与 targeted pytest；最终运行全量 pytest、`git diff --check` 和无网络受控 CLI 验收。

## 不做的事项

- Skill 市场、远端下载、版本管理、签名/发布、团队或跨设备同步；
- 动态脚本执行、目录资源自动执行、任意用户自定义 slash 命令；
- 新 Provider/复杂模型路由、静默模型回退、网络 sandbox；
- 命令级权限或绕过 AgentLoop、Context、Permission、ToolScheduler、MCP 的执行路径；
- RAG、向量数据库、把所有 SOP 预置到 Prompt。

## 验收标准

- AC1：单文件与目录 Skill 的 schema、参数替换、资源 sandbox、资源索引及错误脱敏通过。
- AC2：三级发现、覆盖、冲突、坏项隔离、热更新/失效语义通过；模型启动目录逐项严格只含 name 与 description，绝不泄露其他 metadata。
- AC3：`load_skill` 仅在成功后按需读取并激活 SOP，完整指令每轮最显眼动态注入而不写入 session/JSONL；clear/new/resume 清空激活。
- AC4：shared 与 isolated 模式均保持 Context、Plan/Do、Permission、ToolScheduler、MCP 与会话边界；isolated 覆盖 `history_messages=0`、20 边界及仅非工具消息携带，并只回流安全摘要。
- AC5：工具白名单未知引用失败，多 Skill 交集稳定；`load_skill` 在 Plan/Do 均可见、串行且完整经过 Permission/Scheduler/executor。
- AC6：短命令、静态命令冲突、隐藏、补全、热更新和清除语义通过；静态 `/review` 优先、同名 review Skill 可加载但零 overlay；commit/test 的 `run_command` 在 deny/confirmation 下零绕过。
- AC7：可选模型仅走 Provider 公开受控能力，不可用安全失败且不回退。
- AC8：全量 `pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter11-final"`、compileall、diff check 与 fake CLI 验收通过。
