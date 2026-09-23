# Chapter 11：Skill System 计划

## 架构与数据流

```text
SkillDiscovery (project > user > builtin) -> immutable internal SkillCatalog metadata
                                           -> StartupSkillDirectory(name + description only)
                                           -> activated SkillCommandOverlay/help/completion
AgentLoop system tool load_skill -> serial ToolScheduler/Permission/executor -> SkillLoader -> validated SkillActivation
                                               -> ActiveSkillState
ActiveSkillState -> PromptBuilder dynamic background + SkillToolPolicy
short command -> shared AgentLoop 或 IsolatedSkillRunner -> safe summary only
```

发现只产出 `SkillMetadata`；加载才读取 SOP 和资源索引；激活才产生 `SkillActivation`。`ActiveSkillState` 是 CLI 会话内对象，不序列化到 `ChatSession`、JSONL、Memory 或 artifact。

## 核心模型与接口

| 模型 | 关键字段与约束 |
|---|---|
| `SkillMetadata` | 内部本地模型：name、description、source、root、entry、mode、tools、history_messages、model、parameters、digest；不含 SOP。 |
| `StartupSkillDirectoryEntry` | 仅供模型发现注入：name 与一句 description；不含 tools、mode、model、parameters、路径、digest 或 SOP。 |
| `LoadedSkill` | metadata、参数替换后 SOP、安全相对资源索引；只在 load/activation 中持有。 |
| `SkillActivation` | loaded snapshot、activation sequence、stale/invalid 状态；多个按 sequence 排序。 |
| `SkillCatalog` | 不可变 discovery snapshot、诊断、短命令 overlay；refresh 原子替换。 |
| `SkillToolPolicy` | 普通工具为 Plan/Do 可见工具与激活白名单交集；`load_skill` 是 Plan/Do 都可见、串行的系统例外。 |
| `SkillExecutionResult` | shared/isolated 成功或安全错误、仅可回流的安全摘要。 |

`SkillDiscovery.discover(workspace, user_root, builtin_root, registry)` 返回 catalog；`SkillLoader.load(name, parameters, catalog, registry, provider_capabilities)` 返回 LoadedSkill 或安全错误；`ActiveSkillState.activate/clear/refresh` 管理 snapshot；`IsolatedSkillRunner.run(...)` 只返回安全摘要。

## 模块边界与文件

| 文件 | 责任 |
|---|---|
| `newcode/skills/types.py` | frontmatter/schema、metadata、activation、稳定错误与结果。 |
| `newcode/skills/discovery.py` | 三根扫描、优先级、路径安全、轻量解析、diagnostic、catalog。 |
| `newcode/skills/loader.py` | 按需 SOP/资源加载、参数替换、模型/工具白名单验证。 |
| `newcode/skills/state.py` | 会话级激活、热更新 stale/invalid、动态背景渲染、白名单交集。 |
| `newcode/skills/tool.py` | 系统 `load_skill` Tool adapter；在 Plan/Do 均可见，经正常 ToolRegistry/AgentLoop/Permission/串行 Scheduler/executor。 |
| `newcode/skills/runner.py` | shared/isolated 子对话、有限历史、摘要回流、隔离 cleanup。 |
| `newcode/skills/commands.py` | catalog snapshot 的受控短命令 overlay；不修改静态 registry。 |
| `newcode/resources/skills/{commit,review,test}/SKILL.md` | 三个内置模板及仅需的安全参考资源。 |
| `newcode/prompt/modules.py`、`builder.py` | active skills 在动态背景首位注入。 |
| `newcode/agent/loop.py` | 请求前刷新、`load_skill` 可见性、SkillToolPolicy 工具过滤及 isolated factory 注入点。 |
| `newcode/cli.py`、`newcode/commands/*` | catalog/activation 生命周期、复合命令视图、clear/resume、CLI cleanup。 |
| `tests/test_skills_*.py`、相关 CLI/Agent/Prompt 回归 | 单元、隔离、gate、热更新、端到端覆盖。 |

Provider 不导入 Skill；Skill 不直接导入 MCP manager/client、工具 executor、PermissionManager 或 ToolScheduler。AgentLoop 仍是唯一可发起 Provider/tool 回合的组件。

## 关键流程

### 发现与刷新

1. 建立三根安全路径，不存在即为空；拒绝 root/entry/resource symlink 和逃逸。
2. 每项轻量读取 frontmatter，校验 schema、名称、白名单工具存在性及 mode；计算 digest。
3. 按 project > user > builtin 决定同名赢家，形成不可变内部 catalog；坏项仅诊断；另从赢家投影出仅 name/description 的模型启动目录。
4. 主回合前 refresh；新增只进入可发现 catalog，内容变更标 stale，删除/无效立即 invalid 并从激活/overlay 移除。

### 加载与 prompt

1. `load_skill` 在 Plan/Do 均经 registry、AgentLoop、Permission、串行 Scheduler 与 executor 执行，调用 loader；它不是只读并发工具。成功激活后才创建对应会话级短命令，短命令也只形成受控 load/execution request。
2. loader 读取赢家 entry、替换声明参数、构造资源索引、检查模型能力，成功则 activation snapshot 替换同名旧 activation。
3. state 将所有有效 activation 以固定标签放在 DynamicPromptBackground 的首项；PromptBuilder 每轮重新构造，session 不变。
4. tool policy 将 active whitelist 交集应用到 AgentLoop provider schemas 和执行前可见性：普通工具为 Plan/Do 集合与所有 whitelist 的交集；保留 Plan/Do 均可见、串行的 system `load_skill`。

### isolated

1. `history_messages` 仅允许 0–20：0 不携带主历史；大于 0 时，从主 session 最近端选择最多 N 条已脱敏 user/assistant 非工具消息，工具调用及工具结果一律排除。
2. 创建 transient child session/context/activation；不共享主 session、memory、artifact 或 active skills，子 activation 仅保留本次 isolated Skill snapshot，使用相同安全基础设施与可用 provider model。
3. 运行后把 child 的最终文本经敏感值遮蔽、长度限制和来源标签生成摘要，作为主 session 安全回流；finally 清理 child artifact。
4. 子会话失败只回流安全码/摘要，绝不携带原始子历史或资源正文。

## Phase

### Phase 1：模型、发现与资源 sandbox

创建 types/discovery，完成单文件/目录包、schema、三级覆盖、坏项隔离、digest、路径和资源索引安全，以及只含 name/description 的启动目录投影。测试：`test_skills_discovery.py`、`test_skills_paths.py`。不接 Prompt/Agent/CLI。

### Phase 2：按需加载、激活与 Prompt 注入

实现 loader/state、参数替换、模型/工具校验、热更新/invalid、动态背景最前注入。测试：`test_skills_loader.py`、`test_skills_state.py`、`test_prompt_skills.py`。不执行工具或 isolated。

### Phase 3：系统 load_skill、白名单与 AgentLoop

实现系统 Tool、SkillToolPolicy、Plan/Do 双模式可见性、串行 Scheduler/Permission/executor/MCP gate，保持 Provider 独立。测试：`test_skills_tool.py`、`test_agent_loop_skills.py`及 MCP/Permission 回归。无 CLI 短命令。

### Phase 4：短命令、shared/isolated 与 CLI 生命周期

实现 immutable overlay、短命令冲突/补全、clear/new/resume 激活清空、isolated runner、安全摘要与 cleanup。静态 `/review` 固定行为优先；同名 review Skill 可加载但无 overlay。测试：`test_skills_commands.py`、`test_skills_runner.py`、`test_cli_skills.py`。不改变 Chapter 10 静态命令。

### Phase 5：边界审计与全量验收

只在失败直接证明时最小修复；全量 pytest、compileall、diff check、fake CLI 验收与 skip 原因记录。无 tmux、网络或生产 secret。

## 技术决定

| 决定 | 选择 | 原因 |
|---|---|---|
| 发现 | 内部 metadata 本地保留；模型启动目录仅 name/description，按需全文 | 防止 metadata/SOP 泄露并避免上下文膨胀。 |
| 覆盖 | project > user > builtin | 项目可定制且稳定。 |
| 多 Skill 工具 | 白名单交集 + `load_skill` 例外 | 最小权限、顺序无关。 |
| 热更新 | valid 变更 stale，显式 reload；删除/无效立即失效 | 不静默改变已激活 SOP。 |
| 动态注入 | active skill 最先出现且不写 session | 指令显著且不污染存档。 |
| 模型失败 | 显式指定不可用即失败，无回退 | 防止隐式能力/成本变化。 |
| 短命令 | 成功 activation 的 immutable overlay，不改 sealed registry | 保留 Chapter 10 防线且不把发现变成任意命令。 |
| `/review` 冲突 | 静态 `/review` 优先；review Skill 可加载但不注册 overlay | 维持 Chapter 10 固定行为。 |
| isolated 历史 | 0–20 条最近脱敏 user/assistant 非工具消息 | 明确零历史与工具信息隔离。 |
| 内置模板 | commit/test 可用 `run_command`；review 只读 | 功能不伪装为只读，仍受原有全部 gate。 |
| isolated 回流 | 脱敏、最多 4,000 字符、无第二次 LLM | 子对话隔离且保留可用结果。 |

## 验证约定

每 Phase：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest <targeted tests> -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter11-phaseN"
```

最终：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter11-final"
git diff --check
```
