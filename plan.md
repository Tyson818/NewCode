# Chapter 14：Git Worktree —— 实施计划

## 架构概览

本计划基于当前 NewCode：`SubAgentRunner` 为每个 child 建立独立 AgentLoop/ChatSession/Context/Permission，`ToolContext.workspace_root` 是当前文件工具与 Permission sandbox 根；`run_command` 当前 shell 执行且以 workspace root 为 cwd；CLI 在 SubAgent shutdown 后继续 Hook、Memory、Context、MCP cleanup。当前无 worktree/cwd/清理支持。

新增 Worktree 层仅服务 Definition 的 `isolation=worktree`；shared Definition 和 Fork 保持原样。组件及单向依赖：

```text
worktrees.types/path_safety/config -> worktrees.git/worktrees.manager
                                      -> worktrees.setup
SubAgentRunner -> WorktreeManager -> child ToolContext(workspace_root, cwd, identity)
ToolContext -> Workspace/files/search/Permission/command sandbox
CLI -> WorktreeManager cleanup -> Hook -> Memory -> Context -> MCP (existing order preserved)
```

Provider 不感知 Worktree。Manager 通过统一 Git adapter 以固定 argv 调用本地 Git；adapter 对每条命令强制 `core.hooksPath` 指向 Manager-owned empty dir，并禁用 system/global config 与交互行为，确保包括 `worktree add` 在内的命令不触发 checkout hooks。Git 输出只用于验证和安全状态判定。Agent worker 只持有不可变 Worktree lease/context，不读取父 AgentLoop 或 ChatSession。

## 核心模型与接口

- `WorktreeRequest`: task ID、Definition slug、base revision 和父仓库身份；路径/branch 均由 manager 生成。
- `WorktreeLease`: canonical repo root/common dir、task ID、branch、path、ownership token/version、创建进程与锁身份、状态；不可由模型构造。
- `WorktreeStatus`: validating/creating/ready/running/completed/failed/cancelled/timed_out/cleaned/preserved/cleanup_failed。
- `ChildExecutionContext`: immutable canonical `workspace_root`, `cwd`, `workspace_identity`, `worktree_lease`；shared 模式只填主 workspace identity。
- `WorktreeManager.create(request) -> lease`: 路径/仓库/分支校验、owner marker 和锁、Git add、注册验证；任何不确定失败均不启动 child。
- `WorktreeManager.verify(lease) -> registration`: 只读查询 Git registry/common-dir/top-level/branch/HEAD 和 ownership marker。
- `WorktreeManager.finish(lease, reason) -> outcome`: cancel/timeout/exception 后检查 registration、status/upstream，安全删除 clean tree，否则 preserve。
- `WorktreeManager.cleanup_stale(repo, now)`: 只在三层过滤全通过时清理超龄 manager-owned tree。
- `WorktreeSetupPolicy`: user trust ceiling 与 project request 的交集，包括 exact copy files、ignored files、dependency roots；Chapter 14 hook execution allowlist 固定为空。
- `CommandSandbox`: platform capability contract；要么接收 root/cwd 并证明进程访问约束，要么返回 unavailable，由 Worktree child 禁用 `run_command`。child Git 仅允许经结构化解析和参数限制的只读 `status --no-optional-locks`、`diff --no-ext-diff --no-textconv`、`log`、`show`、`cat-file`、`ls-files`、受限 `rev-parse`；`add`、`commit`、`update-ref` 及其他写操作一律拒绝。当前 Worktree admin dir（含 index/HEAD/config/logs）与 shared metadata 对 child 全部只读，不开放 index/HEAD 写入；本章不支持 child commit，因为没有 task 独占的 objects/refs。raw shell、命令拼接或无法证明参数/ACL/hooks 隔离时，子进程启动前 fail closed。

所有异常映射稳定错误码；原始 Git stderr、环境、stack trace 不进 ToolResult、session 或日志。

## Module Design 与文件组织

```text
newcode/worktrees/__init__.py           对外类型与受控接口
newcode/worktrees/types.py              lease/status/policy/error 类型
newcode/worktrees/paths.py              slug、relative path、branch/ref 及 containment
newcode/worktrees/git.py                固定 argv、超时、受控 env、empty hooks override、只读查询、Git result parse
newcode/worktrees/manager.py            lock、ownership、create/verify/finish/cleanup
newcode/worktrees/setup.py              safe config 合并与 allowlisted 初始化
newcode/worktrees/command_sandbox.py    child run_command 的能力接口与 fail-closed adapter
newcode/subagents/types.py              Definition isolation 字段
newcode/subagents/discovery.py          解析/验证 isolation
newcode/subagents/runner.py             获取 lease、创建 child context、finally finish
newcode/tools/types.py                  ToolContext 的 cwd/identity
newcode/tools/workspace.py              root-bound canonical 路径校验
newcode/tools/file_tools.py             六工具继承 child root/cwd
newcode/tools/search_tools.py           六工具继承 child root/cwd
newcode/tools/command_tool.py           显式 child cwd + capability enforcement
newcode/permissions/normalizer.py       PermissionRequest 使用 child root
newcode/permissions/sandbox.py          sandbox root 固定为 child root
newcode/context/artifacts.py            artifact root 使用 child context root
newcode/memory/store.py                 workspace/fingerprint/cache 身份不跨 root
newcode/cli.py                          注入 manager 并有界清理
.gitignore                              只追加 .newcode/worktrees/
tests/test_worktrees_*.py               Worktree 单元/集成测试
tests/test_subagents_worktrees.py       Runner/AgentLoop/Permission 集成
tests/test_cli_worktrees.py             生命周期和 fake CLI
```

只在实际存在对应缓存的模块增加 canonical root key；不为没有缓存的 Instructions/Prompt 引入额外缓存。

## Phase 划分

### Phase 1：模型、路径、仓库和配置安全（T1–T4）

**目标：** 明确 isolation schema；纯函数校验 directory/branch/path; Git argv/结果模型；Worktree 配置 strict loader 与 user/project allowlist 合并。追加唯一 ignore 行。

**依赖：** 无。先读 `AgentDefinition`、现有 `ToolContext`、`.gitignore` 和 Permission path normalization。

**验证：** schema default/invalid; 分段/长度/盘符/UNC/斜杠/反斜杠; canonical containment; junction/symlink; `git check-ref-format` 临时本地 repo; YAML 单项错误隔离、project 不能扩权/秘密 allowlist deny 优先；argv 列表与 shell=False。

**风险门：** 不实现 Git mutations；如路径/reparse 检查不可靠，本 Phase fail 且不可进入创建阶段。

### Phase 2：WorktreeManager create、ownership 与注册恢复校验（T5–T8）

**目标：** 锁、owner marker、生命周期状态、受控 create、registration validation；仅可复用当前 Manager 仍登记且未占用的同 task lease。

**依赖：** Phase 1。

**验证：** 临时本地 repo worktree create/list/remove; branch/dir 冲突; common dir/path/ref/HEAD/marker 检查; 伪造/重复/丢失注册拒绝; 并发锁; Git failure/timeout; repo/user/global fixture hooks sentinel 必须证明每条 Manager 命令（特别 `worktree add`）零执行；清理只回滚本次已证明创建对象。

**风险门：** 不按目录存在恢复；任何验证不可用时不启动 child、不回退 shared；manager 绝不调用 `--force`、prune、remote。

### Phase 3：cwd/root 贯通、Permission sandbox 和工具路径/cache（T9–T14）

**目标：** 引入 immutable ChildExecutionContext；Worktree root 作为 sandbox，cwd 明确传给所有 file/search/command 工具；缓存按 root identity 隔离；Process command sandbox capability 未验证时禁止 Worktree `run_command`。

**依赖：** Phase 2。

**验证：** 六个内置工具 fake context 观察 root/cwd；所有文件操作拒绝 traversal/absolute/link escape; Permission normalized path root; file cache 两个 worktree 同相对路径无复用；command backend 验证 cwd 和 OS 限制或明确零执行拒绝。Git 命令矩阵只允许受限只读查询，拒绝 `add`、`commit`、`update-ref`、ref/config/worktree 写入及未列命令；检查拒绝前后 shared objects/refs、当前 index/HEAD、main 与其他 Worktree admin metadata 无变化。当前 Worktree admin dir 也不可写。若解析、ACL 或 hooks 隔离不能证明，Git/`run_command` 均不得启动子进程。

**风险门：** 这是最大风险 Phase。仅设置 subprocess cwd 不算隔离；若无法证明进程级 confinement，Worktree Definition 可继续使用非 command 工具，但 `run_command` 必须不可见或 fail closed。shared 模式不变。

### Phase 4：初始化 allowlist 与 Definition Runner/结果整合（T15–T18）

**目标：** user trust ceiling/project subset 的本地配置、安全文件复制、忽略文件精确复制、Git hooks 全面禁用、dependency link 只读验证；Definition Runner 创建 Worktree、注入 SOP/path context、保持 Permission/预算/cancel 和受限结果语义。

**依赖：** Phase 1–3。

**验证：** copy exact allowlist 和 hard-deny secrets/runtime; project expansion 被拒; O_EXCL/atomic cleanup; fixture repo/user/global hooks 均零执行且不复制 hook 源; dependency link unknown/writeable/outside denied; child result sanitized branch/path/status;初始化失败隔离。

**风险门：** defaults 全为空；hooks execution allowlist 固定为空；无法验证 links/权限则跳过该资源，不降低安全标准；不能清理时安全保留并报错。

### Phase 5：完成保护、过期清理、CLI 生命周期（T19–T22）

**目标：** status/ahead 判定、clean-only remove，否则 preserve；三层 stale filter 与跨进程锁；create/cancel/timeout/clear/resume/exit/exception 的 manager 生命周期；cleanup 有界且不阻断 Hook→Memory→Context→MCP。

**依赖：** Phase 2–4。

**验证：** tracked/staged/untracked/ignored file 与目录/leading commit/no upstream/offline/status error/incomplete scan; child ignored file 必须 preserve; 30天边界; fake lock active; symlink、用户 worktree、main/root 外拒绝; CLI EOF/exit/KeyboardInterrupt/startup error; cleanup fault isolation/order。

**风险门：** unknown 一律 preserve；删除前重新检查 owner、registration、activity 和 dirty/upstream 状态；不得递归删除 worktree root。

### Phase 6：Chapter 14 与 Chapter 4–13 回归、静态审计、local Git/CLI acceptance（T23–T26）

**目标：** 全工具/AgentLoop/CLI integration、错误脱敏和边界审计；完成本地 fake acceptance 和完整 pytest。

**依赖：** Phase 1–5 全部通过。

**验证：** compileall；Worktree targeted pytest；Chapter4–13 regression；全量 pytest；diff check；临时 repository E2E；静态依赖/命令审计。

**退出门：** 任何测试、安全审计、共享 Git metadata ACL、Manager Git hooks override 或 platform command confinement 失败均停止 Phase 6；不使用真实远端、tmux、生产 secret 或 Git 全局设置。

## 生命周期数据流

1. AgentLoop 依旧执行 mode → Permission → ToolScheduler → Agent executor；只有允许的 Definition task 才调用 Runner。
2. Runner 依据 immutable request 调 WorktreeManager create；Manager 在固定 repo root 上执行安全 Git argv，确认注册和 owner 后返回 lease。
3. 初始化步骤只处理 allowlisted files；构造 `ToolContext(workspace_root=worktree, cwd=worktree, workspace_identity=lease.identity)`；构造 child Permission sandbox 仍独立。
4. child AgentLoop 完成/失败/cancel/timeout；Runner finally 调 `finish`；Manager 重新验证后清洁删除或安全 preserve。
5. Worker 仅返回脱敏 summary 和 Worktree outcome metadata；SubAgentManager 按 Chapter 13 规则交给父 AgentLoop 主线程安全点。
6. Session close/CLI shutdown 先取消 Worktree child 并有限等待，然后继续现有 Hook、Memory、Context、MCP cleanup。

## 风险与缓解

| 风险 | 缓解/退出门 |
|---|---|
| shell 能绕出 cwd 或修改共享 Git 元数据 | Worktree 不是 OS sandbox；raw shell 不向 Worktree child 开放。只有命令解析、只读 Git allowlist、metadata ACL 与 hooks 隔离均通过测试时才可执行受限查询，否则 Git/`run_command` 子进程零启动。 |
| Git registry 与磁盘目录不一致 | 注册列表、common dir、top-level、ref、HEAD、owner marker 多因子校验；不确定就保留/拒绝恢复。 |
| branch/path collision | manager-generated task ID，独立校验，禁止覆盖/force。 |
| ignored credentials/runtime state 外泄 | checkout tracked-only；精确 allowlist、hard deny、目录复制禁止。 |
| hook/junction 可执行或逃逸 | Manager 每条 Git 命令强制 empty hooks path、禁用 system/global config；child hooks 默认不执行；依赖链接 default empty。 |
| cleanup race/user work/ignored data | locks + task lease + tracked/untracked/ignored 全量复查；unknown 或 ignored child file preserve。 |
| Worktree root 被 Git track | 专门 `.gitignore` 单行并测试 `git check-ignore`；不把其它 `.newcode` 内容忽略。 |
| MCP/config semantics drift | MCP cwd 保持目前 server spawn 语义；不共享 Manager task cwd。 |

## 配置设计

optional user config `~/.newcode/worktree.yaml` 建立 permission ceiling；project config `.newcode/worktree.yaml` 只能请求 user allowlist 子集。配置项只含精确相对 file lists、approved dependency root IDs 与清理年龄上限（年龄下限固定不得低于 spec 的 30 天）。Chapter 14 Git hook execution allowlist 固定为空，配置不得启用/覆盖 hooksPath、Git executable、branch、worktree root、绝对路径、命令或 sandbox disable。坏 user config 禁用可选 setup 行为；坏 project config 忽略该 project 请求；两者都不阻断共享 Agent。

## 决策记录

| 决策 | 方案 | 理由 |
|---|---|---|
| 默认隔离 | Definition 缺省 shared；显式 worktree 才创建 | 兼容 Chapter 13。 |
| Worktree 根 | repo root `.newcode/worktrees/` | 固定、可 gitignore 和验证，跟随该 repo。 |
| reused tree | 仅本 Manager lease 恢复；不跨进程恢复 | 章节非目标含跨会话持久化，减少冒认风险。 |
| 有变更的删除 | 始终 preserve；本章没有确认丢弃入口 | 防止删除用户代码/领先 commit。 |
| run_command | 仅结构化、参数受限的只读 Git allowlist；`add/commit/update-ref` 与全部写命令 deny；解析或隔离不可证明则 deny | index/HEAD 也只读；没有独占 objects/refs，故不支持 child commit。 |
| MCP stdio cwd | 保持现有 NewCode 进程 cwd 语义 | 避免无授权改变 server 配置行为。 |
| env/config/hooks/link | 默认空；hooks execution allowlist 固定空；依赖链接由用户信任上限控制 | 不复制 secret/runtime state；所有 Manager Git 命令强制 hooks off。 |
| Git 命令 | 内部固定 argv/shell=False/no network | 防止任意 Git 命令、命令注入及远程副作用。 |

## 测试与验收命令

每 Phase：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest <该 Phase tests> -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter14-phaseN"
git -c safe.directory=F:/agent/Newcode diff --check
```

最终还须 Chapter 4–14 targeted regression 与：

```powershell
.venv\Scripts\python.exe -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter14-final"
```

人工 acceptance 使用 fake Provider、临时 home/workspace、本地 `git init` fixture 与受控 command sandbox fake；禁止网络、真实 MCP、真实生产 hooks、生产 secret、tmux、push 和全局 Git 配置变更。
