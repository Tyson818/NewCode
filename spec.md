# Chapter 9：Project Instructions、Session Persistence 与 Automatic Memory

## 背景与目标

NewCode 当前只有进程内 `ChatSession`，退出后历史丢失；PromptBuilder 虽预留自定义指令和长期记忆模块，但没有受控来源；Context Management 只管理当前会话的大小。本章增加可审计的项目指令、可恢复的本地会话与保守的自动记忆，使长期工作连续而不越过既有权限、sandbox、Plan/Do 和工具调度边界。

目标是：加载三层指令并确定优先级；以安全 JSONL 存档和恢复会话；在自然完成后异步生成有限、可去重的四类记忆，并在下一次主模型请求前注入相关范围。用户原文在活动会话与 Chapter 8 的近期保留区规则不变。

## 功能需求

### F1：三层项目指令

按低到高加载用户级 `~/.newcode/INSTRUCTIONS.md`、工作区级 `<workspace>/.newcode/INSTRUCTIONS.md`、项目级 `<workspace>/AGENTS.md`。高层与低层冲突时高层优先；注入动态 system message 时必须按高到低排列项目级、工作区级、用户级内容，并保留来源标签和此优先级说明。指令不能作为用户消息写入 `ChatSession`。

指令中的独立一行 `@include relative/path.md` 允许展开。包含路径相对当前指令文件；不得为绝对路径、不得含 `..`，解析后仍必须位于该层允许根（用户层为 `~/.newcode`，工作区/项目层为 workspace）内。最大嵌套深度为 **5**；以规范化解析路径维护 visited 集，重复或环引用只安全跳过一次。文件不存在、不可读、编码错误、越界、环或超深只产生脱敏错误码，不阻断其余层加载或正常对话。

### F2：JSONL 会话归档、恢复与清理

每个 CLI 会话使用固定格式 `YYYYMMDD-HHMMSS-xxxx` 的稳定 session ID：前段为本地创建时间，`xxxx` 必须由安全随机源从 `[a-z0-9]{4}` 生成，用于防止同秒冲突。存档只可位于用户专用根 `~/.newcode/sessions/` 的 `<session-id>.jsonl`；禁止把用户提供的 ID 直接拼为路径，禁止符号链接/绝对路径/`..` 逃逸。若同一秒的候选文件名已存在，只重新生成后缀并最多尝试 16 次；全部冲突或创建失败时返回脱敏安全错误，绝不覆盖、截断或替换已有会话。首行是版本化元数据，后续每行是可 JSON 转发的消息或安全事件。每次自然完成的 turn 与关闭前均写入完整、已脱敏 checkpoint；写入须原子化或保证读者只接受完整记录。

恢复时验证 ID、版本、workspace 绑定和每条记录。坏 JSON、未知 record、字段类型错误或无法反序列化的行安全跳过并统计；不得因单坏行中止可用历史。恢复后的尾部若存在未配对的 assistant tool call、孤立 tool result 或不完整交换，则从第一个不完整交换开始截断；不得把半对交换发送给 Provider。恢复后继续复用 Context Management，且在下一次主模型请求前按既有规则执行预防性外置/必要压缩。

`/sessions` 只列出当前 workspace 可恢复会话的 session ID、标题或安全摘要、最后更新时间和消息数；列表不得包含其他 workspace 的会话内容或标识。`/resume <session-id>` 恢复指定会话。无效、过期、跨 workspace 或不可恢复 ID 只返回脱敏安全提示。恢复若距最后有效更新时间达到 **24 小时**，CLI 显示不含内容的时间跨度提醒。启动和关闭可清理最后有效更新时间超过 **30 天**的 session 文件；只能删除 session 根内已验证的普通归档文件，跳过活动会话、符号链接、坏文件和根外路径，清理失败互相隔离。

### F3：自动记忆

自动记忆仅保存长期有用且可操作的四类笔记：

1. 用户偏好；
2. 纠正反馈；
3. 项目知识；
4. 参考资料。

用户级笔记只位于 `~/.newcode/memory/user/`，可跨工作区使用；项目级笔记只位于 `<workspace>/.newcode/memory/project/`，必须含 workspace 指纹，绝不注入其他项目。每个笔记使用 YAML frontmatter（格式版本、ID、类别、scope、workspace 指纹、创建/更新时间、标签、来源 session 的安全标识）与 Markdown 正文。用户级与项目级索引均最多 **200 行**且最大 **25 KB**；新增、更新、合并与重建索引时必须同时验证两项限制，不能以任一项替代另一项。请求前最多注入 **8** 条且总正文不超过 **6,000** 字符，超限时按更新时间和相关标签稳定选择；该注入限制不得替代索引上限。笔记和索引中的敏感值必须遮蔽。决策和工作流经验只能作为项目知识或参考资料笔记的正文内容，不构成额外类别。

LLM 负责在给定候选笔记与新候选时判定重复、合并、替换或忽略；它不拥有文件、工具、网络或权限能力。模型产出必须是严格可解析的受限结果，格式错误或异常时不写入记忆。自动记忆不得把短期工具输出、完整对话、secret、headers、环境变量、URL credential、完整堆栈或远端错误复制为笔记。

### F4：自然结束后的异步执行

只有 AgentLoop 产生正常最终回答（不是 Provider 错误、取消、权限拒绝、Plan Mode 禁止、最大轮数停止或异常）后，才可提交一次自动记忆任务。任务接收已脱敏、受大小限制的历史快照，异步、串行地运行，绝不阻塞已经交给 CLI 的主回复，也不改变 AgentLoop 的事件顺序。

关闭时停止接收新任务；对正在运行的任务执行有界等待后取消/放弃，绝不为等待记忆阻断 CLI、Context artifact 清理或 MCP shutdown。写笔记必须通过临时同目录文件加原子替换，失败不得留下半成品。重复关闭安全，单项关闭失败不得阻断其余清理。

### F5：请求前 Prompt 注入

每次主模型请求前，PromptBuilder 在稳定系统提示之后、现有 system reminder 之前，注入一条含项目指令与已筛选记忆的动态 system message。注入内容标明“背景信息而非用户输入”，来源、scope、可能过期性及不得绕过权限/工具结果验证；不写入 ChatSession，不参与 JSONL 原文记录，也不改变 Context Management 对近期用户消息的原文保护。

加载失败、没有指令、没有记忆、索引上限或记忆任务失败时，主请求仍正常进行。Provider 不导入或理解 Instruction、SessionArchive 或 Memory；这些功能不能绕过 AgentLoop、Plan/Do gate、PermissionManager、ToolScheduler、ToolRegistry、MCP 或 workspace sandbox。

### F6：安全、错误与隐私

所有持久化、提示注入、CLI 状态和错误均使用既有敏感值遮蔽规则，并额外遮蔽常见敏感字段。稳定、安全的错误码包括 `instruction_load_failed`、`instruction_include_cycle`、`instruction_include_depth`、`instruction_path_outside_workspace`、`session_archive_failed`、`session_restore_failed`、`session_record_invalid`、`session_tool_pair_truncated`、`memory_index_failed`、`memory_note_invalid`、`memory_generation_failed`。不得显示 secret、原始路径凭据、完整堆栈、完整远端响应或原始会话内容。

## 非目标

- 向量数据库、embedding、RAG、语义搜索或机器学习式召回。
- 团队同步、云同步、跨设备共享、远端备份或账户体系。
- 复杂加密密钥管理、复杂 OAuth、真实第三方网络服务。
- 修改 Provider 协议、MCP transport、Permission 规则语义、ToolScheduler 或六个内置工具。
- 自动执行来自指令、会话或记忆的操作，或让记忆模型调用工具。

## 验收标准

- AC1：三层指令的加载顺序为 user < workspace < project，动态注入顺序为 project → workspace → user；5 层 include、visited 防环及 sandbox 防逃逸可由本地 fixture 观察，任一文件失败不阻断其他层。
- AC2：`YYYYMMDD-HHMMSS-[a-z0-9]{4}` session ID 由安全随机源生成；同秒冲突只重试安全后缀、达到 16 次后安全失败且绝不覆盖已有归档。JSONL 写入/恢复、坏行跳过、尾部工具交换截断、24 小时提醒与 30 天受限清理可重复验证。
- AC3：`/sessions` 仅显示当前 workspace 可恢复会话的 ID、标题/安全摘要、最后更新时间和消息数；`/resume <session-id>` 的无效、过期、跨 workspace 或不可恢复输入均不泄露信息。
- AC4：活动会话仍遵守 Chapter 8 的外置与摘要；恢复不会发送不配对工具消息，近期用户原文不被持久化流程改写。
- AC5：用户偏好、纠正反馈、项目知识、参考资料四类记忆，scope 隔离、frontmatter、每个 scope 的 200 行和 25 KB 双重索引上限、8 条/6,000 字符注入上限、LLM 去重和原子写入均可验证。
- AC6：只在自然最终回答后异步排队；主回复、工具顺序和既有 gate 不被阻塞或改变；关闭安全且隔离失败。
- AC7：敏感值、headers、环境变量、URL credential、完整堆栈及远端敏感内容不会进入归档、笔记、索引、prompt、CLI 或错误。
- AC8：全量回归与 fake provider/local fixture CLI 验收通过；不使用真实网络、生产 secret 或第三方 MCP。

## 测试范围

新增指令、会话归档、记忆、Prompt/AgentLoop/CLI 集成测试，全部使用临时 home/workspace、fake clock、fake provider 与本地文件。覆盖恶意 include/ID/符号链接、坏 JSONL、多工作区隔离、崩溃/关闭路径、去重格式失败、动态 prompt 顺序、自然完成与非自然结束、Context Management 和既有 Permission/MCP/Plan-Do 回归。每个 Phase 运行 `python -m compileall newcode` 及其 targeted pytest；最终运行 `pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter9-final"` 与 `git diff --check`。
