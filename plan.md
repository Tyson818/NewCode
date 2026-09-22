# Chapter 9：Project Instructions、Session Persistence 与 Automatic Memory 实施计划

## 架构与调用顺序

```text
CLI startup
  -> 安全解析 workspace / user roots
  -> InstructionLoader + SessionArchive.restore
  -> ChatSession + ContextManager(session_id)
每次 AgentLoop 主请求前
  -> ContextManager.prepare -> PromptContextLoader(指令 + 记忆) -> PromptBuilder
自然 AgentFinalAnswer 后
  -> MemoryService.submit(snapshot) [后台，不等待]
CLI finally
  -> archive checkpoint -> memory shutdown(有界) -> context cleanup -> 原有 MCP shutdown
```

各持久化组件只拥有自己的受控根；所有路径经 `resolve()`、根包含性、文件类型和符号链接检查。动态提示只构造 Provider 消息，绝不写回 `ChatSession`。Provider 继续只接收消息、tools 与 `allow_tool_calls`，不导入本章模块。

## 模块设计

| 模块 | 责任 | 关键接口 |
|---|---|---|
| `newcode/instructions.py` | 三层加载、include 展开、来源/错误 | `load_project_instructions(workspace, user_root)` |
| `newcode/persistence.py` | ID、JSONL checkpoint/restore、30 天清理 | `SessionArchive.create/append/restore/cleanup` |
| `newcode/memory/types.py` | note/frontmatter/index/候选和结果领域类型 | 不依赖 Provider 或工具 |
| `newcode/memory/store.py` | user/project 根、frontmatter、索引、原子读写/清理 | `MemoryStore.load_context/apply` |
| `newcode/memory/service.py` | 有界异步队列、自然完成快照、LLM 去重、shutdown | `submit_after_natural_completion` |
| `newcode/prompt/builder.py` / `modules.py` | 将动态背景插入 stable prompt 与 reminder 间 | 不改变 session |
| `newcode/session.py` | 安全消息序列化/反序列化与 session ID | 保持当前 tool pairing 语义 |
| `newcode/agent/loop.py` | 主请求前获取动态 prompt；自然 final 后通知 service | 不等待 memory |
| `newcode/cli.py` | `/sessions`、`/resume <session-id>`、恢复提醒、checkpoint/finally 编排 | 保持现有命令和 MCP 生命周期 |

## 数据与格式

`SessionArchive` 使用 `YYYYMMDD-HHMMSS-[a-z0-9]{4}` ID；后缀来自安全随机源。同秒名称冲突时只重抽后缀，最多 16 次，以排他创建确保绝不覆盖已有归档；失败只返回安全错误。header 记录格式版本、session ID、workspace 指纹、创建/更新时间；message 记录使用 role/content/tool_calls/tool_call_id 的 JSON-safe 形式，敏感内容先遮蔽。恢复器按顺序保留可验证记录，使用未完成 assistant call 集合识别尾部截断。查询器按 workspace 指纹只返回可恢复的 ID、标题/安全摘要、最后更新时间和消息数。

`MemoryNote` 的类别固定为用户偏好、纠正反馈、项目知识、参考资料，使用受限 YAML frontmatter 和 Markdown 正文。文件名由受控 ID 生成；索引不存正文，只存安全 metadata，且 user/project 两个 scope 各自同时受最多 200 行及最大 25 KB 限制。记忆 LLM 输入是脱敏候选与最多 200 条 metadata，输出只能是 `ignore`、`create`、`update`、`merge` 的 JSON 包络；解析失败不落盘。

动态提示固定为：stable modules → `项目指令与记忆（动态背景）` → system reminder → session messages。该动态背景内的指令严格按 project → workspace → user 排列，随后才是经过 scope 筛选的记忆；它标识层级/来源/范围和“不是授权、必须核验工具事实”。

## Phase

### Phase 1：基础类型、路径安全与三层指令

目标：创建安全根/错误模型与指令加载器。文件：`newcode/instructions.py`、必要的 `newcode/session.py`、`tests/test_instructions.py`、`tests/test_session.py`。依赖：既有 sensitive/sandbox 原则。完成标准：三层优先级、include 5 层、cycle/visited/逃逸/符号链接、脱敏错误通过；不接入 Prompt/CLI。

### Phase 2：JSONL 会话归档与恢复

目标：实现 `[a-z0-9]{4}` 安全随机后缀的 session ID、16 次冲突重试、版本化 JSONL、坏行跳过、尾部工具配对截断、提醒与清理。文件：`newcode/persistence.py`、`newcode/session.py`、`tests/test_session_persistence.py`、必要的 `tests/test_session.py`。依赖：Phase 1 安全路径。完成标准：安全 checkpoint/restore/30 天清理、冲突零覆盖通过；不接入 CLI/AgentLoop。

### Phase 3：记忆存储与安全注入上下文

目标：实现用户偏好、纠正反馈、项目知识、参考资料四类 note、frontmatter、scope/index caps、动态 prompt 数据读取。文件：`newcode/memory/__init__.py`、`types.py`、`store.py`、`newcode/prompt/modules.py`、`builder.py`、`tests/test_memory_store.py`、`tests/test_prompt_memory.py`。依赖：Phase 1。完成标准：隔离、原子写、每个 scope 200 行和 25 KB 双重上限、8/6000 注入上限及指令高优先级在前的顺序通过；不调用 LLM、CLI 或 loop。

### Phase 4：异步记忆服务与自然结束 hook

目标：有界后台任务、严格 LLM 结果解析/去重、自然 final 唯一触发、安全关闭。文件：`newcode/memory/service.py`、`newcode/agent/loop.py`、`tests/test_memory_service.py`、`tests/test_agent_loop_memory.py`。依赖：Phase 2–3。完成标准：主回复不等待、非自然结束零任务、失败零写入、关闭隔离通过；不改 Provider/MCP/Permission。

### Phase 5：CLI 生命周期、恢复和 session 命令

目标：启动恢复/新建、`/sessions`（仅当前 workspace 的 ID、标题/安全摘要、最后更新时间、消息数）、`/resume <session-id>`、checkpoint、时间提醒、清理和 finally 编排。文件：`newcode/cli.py`、必要时 `newcode/persistence.py`、`tests/test_cli_session.py`、`tests/test_cli_memory.py`。依赖：Phase 2、4。完成标准：无效、过期、跨 workspace、不可恢复 ID 安全失败；EOF、`/exit`、中断、loop 异常、启动后异常均安全；保持 `/plan`、`/do`、`/compact`、MCP 行为。

### Phase 6：回归、审计与受控验收

目标：全量验证与最小修复。允许文件仅为直接失败所需最小模块/测试。完成标准：全量 pytest、compileall、diff check、静态依赖审计、fake CLI 流通过；无真实网络/secret/tmux 安装。

## 关键决定

| 决定 | 选择 | 原因 |
|---|---|---|
| 指令优先级 | 加载为 user < workspace < project `AGENTS.md`；注入为 project → workspace → user | 兼容当前项目治理文件，且模型先见高优先级内容 |
| include 限制 | 5 层、同层 root、canonical visited | 防环与路径逃逸 |
| session 位置 | `~/.newcode/sessions` | 与工作区 runtime artifact 分离，避免提交 |
| 记忆位置 | user root + workspace `.newcode/memory/project` | 明确跨项目边界 |
| 异步机制 | 单 worker、有界队列、关闭有界等待 | 不阻塞回复且避免并发写冲突 |
| 注入顺序 | stable 后、reminder 前 | 指令可见，仍由当前运行态提醒收束 |
