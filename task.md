# Chapter 14：Git Worktree —— 原子任务

## 任务约定

- 只在 Chapter 14 获批后实施；每个 Phase 内按编号顺序执行，Phase compileall、targeted pytest、`git diff --check` 全过才进入下一 Phase。
- Git 测试仅用临时本地 repositories/fake executable；不 remote、不 push、不改全局 Git 配置。
- 当前 Git/ToolContext/Permission/CLI 的实际限制以 `spec.md` 为准：特别是 `run_command` 没有 OS sandbox 时必须 fail closed。
- 下表是最大文件范围；某任务没有授权的模块不得顺便修改。新增最小测试 fixture 只能放在对应测试文件。

## 文件清单

| 用途 | 允许新增/修改文件 |
|---|---|
| Worktree 核心 | `newcode/worktrees/__init__.py`、`types.py`、`paths.py`、`git.py`、`manager.py`、`setup.py`、`command_sandbox.py` |
| Definition / child | `newcode/subagents/types.py`、`discovery.py`、`runner.py`、必要时 `manager.py`、`policy.py`、`tool.py` |
| cwd/工具安全 | `newcode/tools/types.py`、`workspace.py`、`file_tools.py`、`search_tools.py`、`command_tool.py`、`newcode/permissions/normalizer.py`、`sandbox.py` |
| 根身份派生状态 | 必要时 `newcode/context/artifacts.py`、`newcode/memory/store.py` |
| CLI lifecycle | `newcode/cli.py` |
| 忽略规则 | `.gitignore`（仅追加 `.newcode/worktrees/`） |
| 单元测试 | `tests/test_worktrees_paths.py`、`test_worktrees_git.py`、`test_worktrees_config.py`、`test_worktrees_manager.py`、`test_worktrees_setup.py`、`test_worktrees_command_sandbox.py` |
| 集成测试 | `tests/test_subagents_worktrees.py`、`tests/test_cli_worktrees.py`、必要时 `tests/test_agent_loop_subagents.py`、`test_tools_workspace.py`、`test_permissions_sandbox.py`、`test_context_artifacts.py`、`test_memory_store.py`、`test_mcp_manager.py` |

`pyproject.toml` 仅当测试直接证明新增 production module 无法打包时允许最小改动；本章默认不新增依赖。

## Phase 1：模型、路径、Git runner 与配置校验（T1–T4）

### T1：Definition isolation schema

- **前置条件：** Chapter 13 AgentDefinition/discovery 测试通过；已读定义 frontmatter 当前校验。
- **允许文件：** `newcode/subagents/types.py`、`newcode/subagents/discovery.py`、`tests/test_subagents_discovery.py`。
- **实现动作：** 增加 `shared|worktree` 字段；缺省 shared；非法值使单 Definition 隔离失败且不阻断 catalog 其它项；Fork 不读取 isolation。
- **测试：** 缺省/两合法值/非法值/无效高优先级隔离；已有 Definition/Fork 兼容。
- **完成判定：** 目录 projection 不暴露额外敏感字段，旧 Agent 行为保持不变。
- **禁止边界：** 不创建 Worktree，不改主 AgentLoop/CLI/Provider/Permission。

### T2：Worktree 名称与路径校验

- **前置条件：** T1 完成；锁定 spec 的 root 和命名规则。
- **允许文件：** `newcode/worktrees/__init__.py`、`types.py`、`paths.py`、`tests/test_worktrees_paths.py`。
- **实现动作：** 校验安全 slug、task ID、slash 段、长度；生成 `<repo>/.newcode/worktrees/<agent-slug>/<task-id>` 与受控 branch；拒绝绝对路径、盘符、UNC、反斜杠、空段、dot 段、链接/junction、非普通文件/目录和 canonical escape。
- **测试：** 分段、最大长度、slash 深度、Windows drive/UNC、POSIX absolute、junction/symlink（环境不支持则只 skip 创建）、root containment 与 branch unsafe 输入。
- **完成判定：** 任意非法输入稳定为 `worktree_path_invalid` 或专用冲突码，无文件副作用。
- **禁止边界：** 不允许模型直接给 filesystem path 或 Git ref；不执行 Git。

### T3：固定 argv Git adapter

- **前置条件：** T2 完成。
- **允许文件：** `newcode/worktrees/git.py`、`types.py`、`tests/test_worktrees_git.py`。
- **实现动作：** 注入可执行文件/fake runner；固定 argv、cwd、受控 env、shell=False、timeout、最大输出；每条 manager 命令强制 `-c core.hooksPath=<manager-owned-empty-dir>`、禁用 system/global config、prompt 和 fsmonitor；包装 repo root/common-dir/head/ref/list/status/ignored/upstream/check-ref-format 只读调用。
- **测试：** fake runner 断言 argv 分项、shell=False、empty hooks override 在所有子命令存在、system/global config 被禁用、无模型参数注入；timeout、异常、非零 exit、畸形/超长输出脱敏。
- **完成判定：** 不调用网络/Git 任意命令，错误只产生稳定安全码。
- **禁止边界：** 本任务无 `worktree add/remove` mutation；不暴露 subprocess stderr。

### T4：用户/项目 Worktree 初始化配置

- **前置条件：** T2–T3 完成。
- **允许文件：** `newcode/worktrees/setup.py`、`types.py`、`tests/test_worktrees_config.py`、`.gitignore`。
- **实现动作：** strict YAML user trust ceiling 和 project subset；未知字段/坏条目隔离；硬拒绝 secrets/runtime state；只向 `.gitignore` 追加 `.newcode/worktrees/`。
- **测试：** 两层顺序、project 不扩大 user allowlist、单项坏配置隔离、敏感名即使显式 allow 也拒绝、`git check-ignore` 临时 repo 验证准确忽略 Worktree root 而不忽略 `.newcode/INSTRUCTIONS.md` 等。
- **完成判定：** config 坏值不会阻断 shared CLI；ignore scope 精确。
- **禁止边界：** 不复制文件、不调用 `git add`、不覆盖已有 `.gitignore` 规则。

**Phase 1 gate：** `compileall newcode`；`test_subagents_discovery.py test_worktrees_paths.py test_worktrees_git.py test_worktrees_config.py` targeted；`git diff --check`。全通过才进入 T5。

## Phase 2：Manager 创建、注册验证与状态（T5–T8）

### T5：Lease、owner marker、锁和生命周期模型

- **前置条件：** Phase 1 全通过。
- **允许文件：** `newcode/worktrees/types.py`、`manager.py`、`tests/test_worktrees_manager.py`。
- **实现动作：** 定义 Worktree lease/status/error/lock interface；ownership marker 存于 canonical common Git dir 的专用 NewCode metadata 子目录，不在 child checkout；marker 含版本、owner token、repo identity、branch/path/task ID；跨进程锁与进程内占用表。
- **测试：** 状态转换合法/非法、marker schema、task lock 双占、锁释放与 owner mismatch。
- **完成判定：** marker 不包含 secret，状态不写入 Chapter13 SessionArchive。
- **禁止边界：** 不扫描/删除任意 Worktree，不在 worker 修改父状态。

### T6：Worktree add 创建路径

- **前置条件：** T5 完成。
- **允许文件：** `newcode/worktrees/git.py`、`manager.py`、`tests/test_worktrees_manager.py`、`tests/test_worktrees_git.py`。
- **实现动作：** 锁定 repo/task；验证 non-bare、canonical root/common dir/base HEAD、目标与 ref 未占用；先登记可回滚 owner lease，再经 T3 hook-safe wrapper 固定 argv 执行 `worktree add -b`；成功后查询并校验注册信息才返回 ready lease。
- **测试：** 临时本地 git init repo 创建成功，两个 worktree branch/path 唯一；repo/bare/invalid HEAD、branch/path collision、git error/timeout fail closed；为 repo `.git/hooks/post-checkout`、user/global hooksPath fixture 放置 sentinel，worktree add checkout 全过程 sentinel 不得创建；不依赖 network。
- **完成判定：** create 成功唯一返回已验证 lease；拒绝不启动 child。
- **禁止边界：** 不用 `--force`、shell 字符串、push/fetch、现存分支覆盖。

### T7：注册校验与已有目录拒绝/受控恢复

- **前置条件：** T6 完成。
- **允许文件：** `newcode/worktrees/git.py`、`manager.py`、`tests/test_worktrees_manager.py`。
- **实现动作：** 比较 porcelain list、show-toplevel、common-dir、symbolic branch、HEAD、owner marker、Manager lease；仅当前 manager 已登记且未占用的同 task 可复验；其他已存在目标统一拒绝。只读查询也经 T3 hooks/config 隔离 wrapper。
- **测试：** 删除登记、伪造 marker、别的 common dir、branch/path/head 改变、重复 registration、detached HEAD、已占用、单凭目录存在恢复等拒绝；list/rev-parse 查询不触发 fixture hooks。
- **完成判定：** 每项拒绝无删除、无启动、无复用副作用。
- **禁止边界：** 不做跨进程 resume，不调用 prune。

### T8：安全回滚和失败隔离

- **前置条件：** T6–T7 完成。
- **允许文件：** `newcode/worktrees/manager.py`、`git.py`、`tests/test_worktrees_manager.py`。
- **实现动作：** create 半途失败时，只对本 manager-created、marker+registry 双重证明、无用户变更且未占用资源做 remove；其它情况 preserve+diagnostic。
- **测试：** add 前后每阶段注入故障、并发 race、registration 失败、cleanup 命令失败；相邻任务仍可创建/运行。
- **完成判定：** rollback 永不删除先存目录、分支或他人 worktree。
- **禁止边界：** 禁止递归删除公共 Worktree root、force remove、自动 branch -D。

**Phase 2 gate：** `compileall newcode`；`test_worktrees_git.py test_worktrees_manager.py` targeted；`git diff --check`。通过后进入 T9。

## Phase 3：cwd、sandbox、内置工具与缓存（T9–T14）

### T9：不可变 child execution context

- **前置条件：** Phase 2 通过。
- **允许文件：** `newcode/tools/types.py`、`newcode/subagents/types.py`、`newcode/worktrees/types.py`、相应类型测试。
- **实现动作：** ToolContext 增加 canonical `cwd`、workspace identity；旧构造保持 cwd 默认为 workspace root；Worktree root 与 cwd 一致但语义独立。
- **测试：** 默认兼容、相对/非 canonical cwd 拒绝或归一、安全 root identity 不可变。
- **完成判定：** 不调用/修改 process cwd；旧 caller 可继续构造 ToolContext。
- **禁止边界：** 不全局 `chdir`，不在 ToolContext 放入可变父 session。

### T10：workspace resolver 与文件/搜索工具

- **前置条件：** T9 完成。
- **允许文件：** `newcode/tools/workspace.py`、`file_tools.py`、`search_tools.py`、对应既有测试及 `tests/test_subagents_worktrees.py`。
- **实现动作：** 统一使用 `workspace_root` 解析文件参数；固定 child root containment，扫描时拒绝/跳过逃逸 symlink/junction；find/search 返回 child-relative path。
- **测试：** read/write/replace/find/search 五工具在不同 temp worktree 执行、绝对/`..`/链接 escape 拒绝、主目录同名文件保持不变。
- **完成判定：** 六内置工具中的五个文件类工具的所有读写均限 child root。
- **禁止边界：** 不改变 shared tool schema/语义，不读 common `.git` 元数据。

### T11：Permission 与 sandbox root

- **前置条件：** T9–T10 完成。
- **允许文件：** `newcode/permissions/normalizer.py`、`sandbox.py`、相关 sandbox/permission tests。
- **实现动作：** permission request、normalized absolute path 与 sandbox root 一致使用 child workspace root；保持 hard deny、显式 deny、Plan/Do 优先级。
- **测试：** main path 从 worktree child deny；child path allow/确认照既有策略；symlink root escape deny；MCP/non-file policy unchanged。
- **完成判定：** common Git dir/main workspace 不会进入 sandbox allowed root。
- **禁止边界：** 不改变 Permission rules、MCP 或 Plan/Do semantics。

### T12：根身份化 child read cache

- **前置条件：** T9–T11 完成。
- **允许文件：** `newcode/subagents/runner.py`、`types.py`、`tests/test_subagents_worktrees.py`、`tests/test_subagents_runner.py`。
- **实现动作：** 缓存 key 加 canonical workspace root、workspace identity、canonical file path、stat signature；每个 Worktree/task 独立 cache。
- **测试：** 两个 root 相同相对路径不同内容、一个 root 文件变化、cache eviction/invalidation、shared path 不读 Worktree cache。
- **完成判定：** 目录不同绝不 cache hit；existing permission check 仍先于 cache hit。
- **禁止边界：** 不跨 child/task 共享缓存。

### T13：run_command sandbox capability

- **前置条件：** T9 完成；先验证目标平台技术能力，不以 cwd 假称隔离。
- **允许文件：** `newcode/worktrees/command_sandbox.py`、`newcode/tools/command_tool.py`、`types.py`、`tests/test_worktrees_command_sandbox.py`。
- **实现动作：** command tool 从 ToolContext 获取固定 cwd；Worktree child 只可经结构化解析器和 capability 执行受限只读 Git allowlist：`status --no-optional-locks`、`diff --no-ext-diff --no-textconv`、`log`、`show`、`cat-file`、`ls-files`、受限 `rev-parse`。拒绝 `git add`、`git commit`、`git update-ref`、branch/ref 写入、checkout/switch、reset、clean、fetch、push、config、worktree add/remove/prune 和未列命令。当前 Worktree admin dir（含 index/HEAD/config/logs）及 common metadata 对 child 一律只读；不支持 child commit（没有 task 独占 objects/refs）。不把原始 shell 字符串交给 shell；拒绝 operator、pager、external diff/textconv、helper 和 config override。不能证明解析、ACL、参数及 hooks 隔离时，命令执行前 fail closed；shared 保持既有行为。
- **测试：** fake backend 记录 cwd=root；验证只读 allowlist 命令可用；逐项尝试 add/commit/update-ref/ref/config/worktree 写命令与未列命令，断言拒绝且 subprocess 零调用或零副作用；确认 common objects/refs、当前 index/HEAD、main 与其他 Worktree admin metadata 均未改变。覆盖 shell operator/命令拼接不能绕过策略。真实隔离 backend（若实现）以临时本地 repo 验证；不能证明 ACL/解析时断言 Git/`run_command` subprocess 零调用。
- **完成判定：** 只有只读命令矩阵、共享与 task admin metadata 写保护、hooks 隔离全部通过才显示/执行受限 Git 查询；`git add`、`git commit`、`git update-ref` 永远拒绝，不能仅依赖 cwd 或静态文本扫描，也不能 silent fallback。
- **禁止边界：** 不把静态 shell 文本扫描单独视作完整隔离，不运行真实危险命令。

### T14：派生缓存/Context artifact 根隔离

- **前置条件：** T9–T12 完成。
- **允许文件：** 必要时 `newcode/context/artifacts.py`、`newcode/memory/store.py`、`newcode/instructions.py`、`newcode/skills/discovery.py`、SubAgent 相关 tests。
- **实现动作：** 所有已有缓存 key 包含来源 canonical root/workspace fingerprint；project memory fingerprint 使用 worktree root；context artifact 写入/cleanup 锁定当前 child root。没有现存 cache 的模块不新增 cache。
- **测试：** artifact 仅写 child root 并 cleanup；memory project scope 区分同 repo 的不同 worktree；instructions/project discovery 正确 root；无越界清理。
- **完成判定：** root 变化时不复用 derived content；user scope仍按当前规则。
- **禁止边界：** child 不启用 Memory/Archive/Hook/父 Skills，除 Definition SOP 的既有语义。

**Phase 3 gate：** `compileall newcode`；`test_worktrees_command_sandbox.py test_subagents_worktrees.py test_tools_workspace.py test_permissions_sandbox.py test_context_artifacts.py test_memory_store.py` targeted；`git diff --check`。若特定既有测试文件名/结构不同，先定位同等现存测试，不创建重复测试集。

## Phase 4：初始化、安全 Runner 与结果（T15–T18）

### T15：精确配置文件 allowlist 复制

- **前置条件：** Phase 1–3 全通过。
- **允许文件：** `newcode/worktrees/setup.py`、`tests/test_worktrees_setup.py`。
- **实现动作：** user exact allowlist ∩ project request ∩ hard-safe names；copy files / ignored files 仅逐个普通文件复制，不递归；临时文件+原子 rename，reject links。
- **测试：** allow/deny precedence、`.env`/token/session/memory/artifact/MCP config拒绝、路径穿越拒绝、copy failure无半成品、目录不递归。
- **完成判定：** 默认复制 0 项，秘密永不进入 child。
- **禁止边界：** 不复制全部 ignored/untracked、环境变量、父 session。

### T16：dependency link 信任检查

- **前置条件：** T15 完成。
- **允许文件：** `newcode/worktrees/setup.py`、路径工具、`tests/test_worktrees_setup.py`。
- **实现动作：** 只解析用户指定的可信只读 dependency roots；project 只能引用登记 ID；target path canonical、可读、不可写且不能被 child 修改，平台不支持 read-only link 则跳过。
- **测试：** unknown/outside/writable/link-chain/reparse target拒绝；合法 fake trusted root；子 Agent 不能写目标的验证。
- **完成判定：** 默认无 link，失败继续但依赖不可用并记录 safe code。
- **禁止边界：** 不 link `.venv` 或任意 workspace/用户目录，不信任 project 提供的绝对路径。

### T17：Git hooks 显式禁用

- **前置条件：** T15–T16 完成。
- **允许文件：** `newcode/worktrees/setup.py`、`git.py`、`manager.py`、`tests/test_worktrees_setup.py`。
- **实现动作：** Chapter 14 hook execution allowlist 固定为空；Git adapter 对每条 SubAgentManager 命令固定注入 manager-owned empty hooks dir，并禁用 system/global config；忽略 repo/user/global 设置的 hooksPath。checkout、add、list、status、remove 及失败清理统一走该 wrapper。
- **测试：** 为 repo `.git/hooks`、user hooksPath 和 global hooksPath 配置写入 fixture sentinel；逐种 Manager Git 命令均验证 sentinel 零执行，重点覆盖 `worktree add` 的 checkout hook；配置覆盖缺失时断言 Git 命令零启动。
- **完成判定：** 所有 manager Git 入口都可证明 hook 绝不执行；不支持的环境 fail closed。
- **禁止边界：** 不复制/执行任何 hook，不改仓库/用户/全局 Git 配置，不运行生产 hooks。

### T18：Runner 创建/完成集成与安全结果

- **前置条件：** T5–T17 完成。
- **允许文件：** `newcode/subagents/runner.py`、必要时 `manager.py`/`types.py`、`tests/test_subagents_worktrees.py`。
- **实现动作：** Worktree Definition start 前创建并 verify lease，初始化后再建 child ToolContext/AgentLoop；finally 取消/有限等待、finish；结果带脱敏状态/branch/path，保留 Chapter13 budgets/Permission/policy/notifications。
- **测试：** create failure 不启动 Provider；Definition shared旧路径无变；worktree child cwd/root；final/exception/cancel/timeout分别清理或保留；结果不包含 Git output/secrets。
- **完成判定：** 主 ChatSession 不被 worker 修改，Manager安全队列负责结果。
- **禁止边界：** Fork 不创建 Worktree；不合并、不提交、不 push、不变更 Provider。

**Phase 4 gate：** `compileall newcode`；`test_worktrees_setup.py test_subagents_worktrees.py` targeted；`git diff --check`。

## Phase 5：完成保护、过期清理与 CLI 生命周期（T19–T22）

### T19：dirty/upstream outcome

- **前置条件：** Phase 4 通过。
- **允许文件：** `newcode/worktrees/git.py`、`manager.py`、`types.py`、`tests/test_worktrees_manager.py`。
- **实现动作：** `status --porcelain=v1 -z --untracked-files=all` 检查 tracked/staged/untracked；另执行 `status --ignored=matching`、`ls-files --others --ignored --exclude-standard -z` 与 no-follow root walk 检查 ignored 文件和目录；只清理 owner manifest 中逐项验证的本 task temp 再重查。validated upstream rev-list 判断 ahead；无 upstream/离线/timeout/异常/输出截断/扫描错误均 unknown preserve。
- **测试：** clean/no changes, staged/unstaged/untracked/ignored file/ignored directory, ahead commit, no upstream, status/traversal incomplete, network not attempted；child 创建 ignored file 后不得自动删除，tree 必须 preserve。
- **完成判定：** 任一非空/unknown/扫描不完整均不能 remove；Worktree `.git` pointer 仅在与精确 task admin registration 匹配时视为结构性 metadata。
- **禁止边界：** 不自动 commit/reset/clean/stash/fetch/push。

### T20：三层过期清理

- **前置条件：** T19 完成。
- **允许文件：** `newcode/worktrees/manager.py`、`git.py`、`paths.py`、`tests/test_worktrees_manager.py`。
- **实现动作：** 30 天候选 + path gate + Git/owner gate + activity/cleanliness gate；重检后逐项验证 tracked/staged/untracked/ignored 文件/目录及完整 root walk 为空才 remove，记录安全失败继续其余候选。
- **测试：** 恰好/超过 30 天；main/user/root/outside/link/unknown owner拒绝；active lock skip；dirty/ahead/no upstream/ignored file or dir/scan error保留；一个 cleanup失败不阻断其它资源。
- **完成判定：** 清理不遍历/删除公共 root 或 Git metadata；每个删除对象均有完整审计条件。
- **禁止边界：** 不以 mtime 单独授权，不 `rmtree`，不 prune。

### T21：CLI SubAgent/Worktree lifecycle

- **前置条件：** T18–T20 完成。
- **允许文件：** `newcode/cli.py`、`tests/test_cli_worktrees.py`、必要时 `tests/test_cli_subagents.py`。
- **实现动作：** CLI 创建/注入每进程 WorktreeManager；clear/resume 先关闭旧 scope 和 child worktree，再开新 generation；EOF/exit/KeyboardInterrupt/异常统一有限等待，cleanup fail 不阻断 Hook→Memory→Context→MCP。
- **测试：** 所有退出入口、初始化中异常、clear/resume、多个 task隔离、shutdown顺序及异常注入。
- **完成判定：** Manager 每 task资源最多 finish 一次；已有 SubAgent/Hook/Memory/Context/MCP语义不变。
- **禁止边界：** 不新增 slash command、跨会话恢复、MCP cwd更改。

### T22：AgentLoop通知与结果格式回归

- **前置条件：** T18–T21 完成。
- **允许文件：** `newcode/agent/loop.py` 仅测试明确证明需适配时；`tests/test_subagents_worktrees.py`、`tests/test_agent_loop_subagents.py`。
- **实现动作：** Worktree outcome 随 task summary安全传递；通知仍只在主 AgentLoop安全点按序一次性写入正确 session/generation。
- **测试：** completion/collect race、clear后旧通知丢弃、路径/branch脱敏与长度、Context estimate含 notification、Archive不记录task state。
- **完成判定：** 不改变已有 AgentLoop事件/tool-call次序与 gate。
- **禁止边界：** 不允许 worker写 parent ChatSession，不绕过 ToolScheduler/Permission。

**Phase 5 gate：** `compileall newcode`；`test_worktrees_manager.py test_subagents_worktrees.py test_cli_worktrees.py test_agent_loop_subagents.py test_cli_subagents.py` targeted；`git diff --check`。

## Phase 6：全回归与验收（T23–T26）

### T23：Chapter 14 targeted regression

- **前置条件：** Phase 1–5 全通过。
- **允许文件：** 默认无；只允许直接失败证明的 Worktree 生产模块及对应最小回归测试。
- **实现动作：** 运行 Worktree、SubAgent、Tool、Permission、Context、Memory、MCP、CLI targeted suite。
- **测试：** 使用 fake provider、local repo 和 local fixtures，覆盖共享 Git metadata 只读 object/禁止共享 refs 等写入、Manager commands 禁 hooks、ignored 内容保留；记录每个 skip 原因。
- **完成判定：** 所有 targeted 通过；无范围外修复。
- **禁止边界：** 不真实网络/secret/MCP/hooks/tmux。

### T24：全 Chapter 4–14 和全量 pytest

- **前置条件：** T23 通过。
- **允许文件：** 默认无；只修直接回归。
- **实现动作：** compileall、Chapter4–14 regression 与 full pytest，basetemp 使用系统 TEMP。
- **测试：** `.venv\Scripts\python.exe -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter14-final"`。
- **完成判定：** 全量通过并解释所有 skip。
- **禁止边界：** 不忽略/隐藏失败，不扩大测试范围外功能。

### T25：静态安全边界审计与 diff

- **前置条件：** T24 通过。
- **允许文件：** 默认无；审计证明缺陷才最小修复。
- **实现动作：** 审计 Provider无 Worktree 感知、Git argv 与 empty hooks override、安全目录、cwd/sandbox根、只读 Git 命令 allowlist/写命令 denylist、process sandbox 对 common objects/refs 与当前/其他 Worktree admin metadata 的只读边界及 fail-closed、Permission/Plan/ToolScheduler链、无主 session写入、cache root隔离、tracked/staged/untracked/ignored 全状态清理门、MCP语义不变、shared行为兼容。
- **测试：** 临时本地 repo 验证只读 Git allowlist；对 `git add/commit/update-ref`、ref/config/worktree 写入及未列命令断言拒绝且无文件/object/ref/index/HEAD 副作用；`git diff --check`；rg/源码审计记录具体证据。
- **完成判定：** 每项边界均有实现位置与测试佐证；未知标记未完成而非通过。
- **禁止边界：** 不暂存/提交/全局配置。

### T26：本地 Git + fake CLI acceptance

- **前置条件：** T23–T25 通过。
- **允许文件：** 默认无；如流程缺陷仅加最小 acceptance regression test 与直接修复。
- **实现动作：** 临时 home/workspace/local Git repo/fake Provider/command sandbox跑 shared与worktree Definition；工具读写隔离；完成保护、保留信息、cancel/timeout、clear/resume、EOF/exit/error cleanup；确认无网络命令。
- **测试：** 本地 acceptance fixture 和最终 `git status --short`（只读）。
- **完成判定：** main repo文件不被 Worktree child写入；Worktree cleanup/preserve结果与 Git 实际状态一致；无真实 tmux时说明环境原因。
- **禁止边界：** 不修改全局 Git config、不 remote/push、不运行生产 hooks、不给 run_command 未验证 sandbox 权限。

## 执行顺序

```text
T1 -> T2 -> T3 -> T4
                 -> T5 -> T6 -> T7 -> T8
                                  -> T9 -> T10 -> T11 -> T12 -> T13 -> T14
                                                                   -> T15 -> T16 -> T17 -> T18
                                                                                         -> T19 -> T20 -> T21 -> T22
                                                                                                              -> T23 -> T24 -> T25 -> T26
```

每 Phase 的最后一项必须先完成 targeted pytest、`python -m compileall newcode`、`git diff --check`。任何 sandbox confinement、Git ownership、删除保护或权限链无法证明时停止并报告，不允许跳过退出门。
