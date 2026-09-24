# Chapter 14：Git Worktree —— 子 Agent 工作目录隔离

## 背景与当前实现

NewCode 的 Chapter 13 已提供 Definition/Fork 子 Agent、受限 SubAgentManager、独立 child ChatSession/Context/Permission state、工具交集策略以及 AgentLoop 主线程结果通知。当前 `ToolContext` 只有 canonical `workspace_root`，没有独立 `cwd`；六个文件/搜索工具从 `workspace_root` 建立 Workspace，`run_command` 使用 `subprocess.run(..., cwd=workspace_root, shell=True)`。Permission 的 workspace sandbox 对结构化文件路径检查 workspace root，但它不是操作系统级文件隔离。

当前没有 Worktree 管理器、Worktree 恢复验证、child cwd、跨根缓存隔离或环境复制策略。MCP stdio 参数不含 cwd，当前语义是子进程继承 NewCode 进程工作目录。Chapter 14 不得声称这些能力已经存在。

## 目标

为显式声明 `isolation: worktree` 的 Definition child，在同一非 bare Git repository 中创建唯一 Git worktree 和专属分支；子 Agent 的文件工具以该 worktree 为 sandbox 根，所有进程 cwd 显式绑定该 worktree，不使用 `os.chdir`。主 worktree 的项目文件不被该 child 的文件工具修改。`isolation: shared` 保持 Chapter 13 行为不变。

Worktree 是 Git 工作树隔离，不是恶意代码的 OS sandbox。不能证明命令进程、链接目标或配置副作用受限时，相关能力必须 fail closed；不得将固定 cwd 描述为足以限制任意 shell 的安全边界。

## 术语与固定默认值

- **主 workspace**：CLI 启动时 canonical 的项目目录；**repository root**：`git rev-parse --show-toplevel` 返回并校验的根；**common Git dir**：`git rev-parse --git-common-dir` 解析后的 canonical Git 管理目录。
- **Worktree root**：固定为 `<repository-root>/.newcode/worktrees/`。Phase 1 必须只在 `.gitignore` 中追加精确规则 `.newcode/worktrees/`，不忽略 `.newcode/` 整体，不覆盖或清理已存在的用户内容。
- **child sandbox root 与 cwd**：Worktree child 都是该 task worktree 的 canonical absolute path；共享模式两者仍为主 workspace root。两个字段分开传递，禁止依赖进程级 cwd。
- Definition frontmatter `isolation` 允许 `shared` 或 `worktree`，缺省为 `shared`；非法值使该 Definition 按 Chapter 13 的单文件隔离规则跳过，不降级成 shared 执行。
- Worktree 目录相对 Worktree root 的名字由管理器生成：`<agent-slug>/<task-id>`。slug 使用 `[a-z0-9][a-z0-9_-]{0,47}`；task ID 使用 Chapter 13 生成的安全 task ID 字符集且长度不超过 64。目录名最多 2 段，以 `/` 分隔；通用 validator 可测试最多 3 段，但生产 WorktreeManager 只接受上述两段格式。总相对长度不超过 160 字符。
- Git 分支固定为 `newcode/subagent/<task-id>`，不是模型可设置字段。branch 在 argv 参数中独立通过 `git check-ref-format --branch` 校验；同名存在即冲突失败，不删除、覆盖或强制复用。
- 所有 Git 命令使用固定可执行文件和 argv 列表，`shell=False`、固定 cwd、有限超时与受控环境；禁止拼接命令字符串、`shell=True`、模型提供参数或隐式 fetch/push。
- 每条 SubAgentManager 发起的 Git 命令（包括 `worktree add`、checkout/list/status/remove 和失败清理）均显式覆盖 hooks 配置：在 argv 中固定 `-c core.hooksPath=<manager-owned-empty-hooks-dir>`，并禁用 system/global Git config、交互式 prompt 与 fsmonitor；empty hooks dir 必须由 Manager 创建、校验为空且 child 不可写。仓库 `.git/config`、用户 config 或全局 config 中的 `core.hooksPath` 均不得覆盖此设置。无法证明配置覆盖生效时不运行 Git 命令。

## 功能需求

### F1：定义与兼容

Definition schema 新增 `isolation`，值为 `shared|worktree`，默认 `shared`。Fork 始终保持 Chapter 13 的快照行为，不自动创建 Worktree；本章不提供 Fork + Worktree 的组合。Definition 的模型、工具 allow/deny、权限、轮次与 SOP 语义不变。无 isolation 字段的既有 Agent 完全兼容。

### F2：仓库发现及输入校验

启动 task 前确认 workspace 位于有效、非 bare Git repository 中，解析 canonical repository root/common dir/base commit。所有解析输出必须是单行、无控制字符且符合预期格式。Git 缺失、仓库损坏、detached/无效 HEAD、权限错误、超时或输出无法解析均返回稳定安全错误，不创建目录、不退回 shared。

路径解析先验证原始相对名称，再对 root 与候选路径做 canonical containment；逐段拒绝空段、`.`、`..`、绝对路径、盘符、冒号、反斜杠、控制字符、符号链接、junction/reparse point、非普通父项及 root 外路径。平台无法识别或验证 reparse point 时拒绝。目标目录或分支已存在时不覆盖。

### F3：Worktree 创建、归属与受控恢复

WorktreeManager 创建目录前取得仓库级锁和 task 锁，验证目标不存在、branch 不存在，写入仅供管理器使用的 ownership marker，再通过 hooks 显式禁用的 `git worktree add -b <branch> <path> <base-commit>` 创建；特别地，add/checkout 即使会触发 post-checkout 等 hook，也必须使用 Manager-owned empty hooks dir。失败必须回滚只删除本次创建、仍可证明归属且未含用户数据的目标。不得调用 `git worktree prune`、`--force`、任意 branch 删除或 Git 命令代理。

目录已存在绝不构成恢复依据。恢复只允许当前进程内由该 Manager 创建、已登记且未被占用的同 task 记录；如产品需跨进程恢复，应独立审批，不属于本章。管理器仍须进行只读注册验证：`git -C <repo> worktree list --porcelain`，并在候选 worktree 上运行 `git rev-parse --show-toplevel`、`git rev-parse --git-common-dir`、`git symbolic-ref --quiet --short HEAD` 与 `git rev-parse HEAD`。canonical top-level/common-dir、预期 branch、marker task/owner、HEAD 和 Manager 记录必须一致。任一查询失败、Detached HEAD、branch/path 冲突、marker 不合法、注册缺失/重复或 task 已占用均拒绝复用。所有查询只对临时本地 Git 仓库测试，无网络。

### F4：生命周期与状态

每项资源状态至少为：`requested → validating → creating → ready → running → completed|failed|cancelled|timed_out`；完成后 Worktree 结果为 `cleaned|preserved`。验证/创建阶段还可转 `cleanup_failed`。非法转换返回稳定错误码。Worktree 状态与 Chapter 13 task 状态分开保存，task 终态不得伪造 Worktree 已清理。

创建成功后才启动 child；工具 cwd/root 注入完成后进入 running。cancel/timeout 先设置 child cancel 标志并按 Chapter 13 有界等待，再由同一 Manager 串行清理/保留。创建失败只影响本 task。CLI `/clear`、`/resume`、EOF、`/exit`、KeyboardInterrupt、异常及正常退出关闭旧 task scope；活动 Worktree 先取消并有限等待。任何清理步骤异常不能阻断 Hook、Memory、Context、MCP 的既有关闭流程。

### F5：完成后的保留/删除

task 结束后先验证归属，再计算改动：`git status --porcelain=v1 -z --untracked-files=all` 检测 tracked/untracked/冲突改动；另用 `git status --porcelain=v1 -z --ignored=matching` 与 `git ls-files --others --ignored --exclude-standard -z` 检查 ignored 文件/目录，并用不跟随链接的有界文件系统遍历校验 Worktree root 无未枚举内容。状态不得被 `.gitignore` 隐藏。只有 manifest 中逐项登记、由当前 task 创建且已验证身份的临时文件可先安全清理，再重新执行全部状态检查；除此之外任何 ignored/untracked 内容（含 ignored 目录）均意味着 preserve。Worktree 根部 Git 写入的 `.git` pointer file 不是 task 文件，但必须与注册记录指向的 task-specific Git admin directory 精确匹配，不能作为可删除内容。

是否存在领先 upstream 的 commit 由 `git rev-list --count @{upstream}..HEAD` 判定。参数必须由 Manager 固定生成，不接受输入。无 upstream、远端配置不可用、任何 Git 查询/目录遍历失败或超时、输出截断/无法完整解析、锁冲突或归属不明一律视为“状态未知并保留”。本章不访问网络，也不执行 fetch。

仅当工作树无 tracked/staged/untracked/ignored 内容、无领先 upstream 的 commit 且注册/ownership 均有效时，Manager 才可以正常自动移除。存在任意改动、未登记 ignored/untracked 内容、领先 commit、无 upstream 或状态未知时设为 `preserved`，并将脱敏的 canonical 路径、branch 与原因码加入 child 安全结果；不把 Git status 的文件名、diff、凭据或异常文本回灌模型。Chapter 14 不提供丢弃式删除/确认命令；任何需要删除有改动/领先 commit 的 tree 必须 fail closed。

### F6：显式 cwd 与工具安全链

新增明确的 child execution context，至少含 canonical `workspace_root`、canonical absolute `cwd` 和 workspace identity。AgentLoop/ToolContext/Permission request 必须使用 child 的值；不使用 `os.chdir`。permission sandbox root 必须是当前 child worktree root，不能用 common Git dir 或 main workspace 放大访问范围。

`read_file`、`write_file`、`replace_in_file`、`find_files`、`search_code` 均基于 child Workspace resolver；相对路径穿越、绝对路径、symlink/junction 逃逸必须拒绝。`run_command` 的 subprocess cwd 必须显式是 child cwd。**但现有 `shell=True` 命令执行不是 OS sandbox**：Worktree 本身不能阻止 shell 使用 `..`、绝对路径、脚本解释器或子进程访问 main workspace。Worktree child 的 `run_command` 只有在平台 backend 能通过隔离测试证明进程文件访问被限制在 Worktree root 时才允许执行；否则工具在 child registry 中不可用/每次调用 fail closed，绝不退回主 cwd。共享 Agent 的现有命令语义保持不变。

工具仍经过 AgentLoop、Plan/Do、PermissionManager、ToolScheduler、executor。Worktree lifecycle Git 操作是固定内部能力，不暴露给模型为任意 Git 命令；安全策略拒绝时不得创建/删除资源。

#### Worktree `.git` 指针与共享 Git 元数据隔离

Worktree 根的 `.git` 是指向 common Git dir 下 task-specific admin dir 的普通 pointer file。child 可读取该 pointer、当前 Worktree 的 admin metadata（包括 HEAD/index）及 common object database，以执行明确列出的只读 Git 查询；这些读取权限不意味着任一 Git metadata 可写。

Git 命令采用固定 allowlist：仅允许经命令解析器识别并限制参数的 `git status --no-optional-locks`、`git diff --no-ext-diff --no-textconv`、`git log`、`git show`、`git cat-file`、`git ls-files` 与受限 `git rev-parse` 只读查询。不得开放 `git add`、`git commit`、`git update-ref`、branch/ref 写入、checkout/switch、reset、clean、fetch、push、config、worktree add/remove/prune，或任何未列出的 Git 子命令。`git add` 会写当前 index 且可能写共享 object database；`git commit` 需要写 objects 与共享 refs；本章没有 task 独占的 object/ref store，因此不支持 child commit，也不把 Worktree 描述为支持提交。

当前 Worktree 专属 admin dir（包括其 HEAD、index、config、logs 与其他 metadata）对 child 一律只读；不得写入该目录，更不得访问或修改 main/其他 Worktree 的 admin dir。common objects、refs/packed-refs、common config/hooks、main workspace metadata 同样不可写。child 的 `run_command` 不得把原始 shell 字符串交给 shell：只能经结构化解析与 capability 执行上述只读查询，拒绝 shell operator、pager、external diff/textconv、helper、config override 及命令拼接。若无法证明解析、参数限制、路径 ACL 与 hooks 环境均有效，则 Worktree child 的 Git 查询及 `run_command` 必须在启动子进程前 fail closed；仅设置 cwd 不构成隔离证明。

Manager 自身 Git 调用使用独立的受控 capability，不继承 child sandbox 的写权限，但只允许本 spec 固定的 lifecycle argv。所有 Manager Git 调用都通过一个 central wrapper 注入 empty hooks path，并禁用 system/global config 和交互行为；其权限也不能写 main workspace 内容、用户配置或全局配置。child 通过 `run_command` 发起 Git 时必须走 sandbox 提供的 Git capability wrapper；不能保证共享 metadata ACL 与 hooks 环境时零执行。

MCP adapter 不改变。stdio MCP 继续采用现有配置语义（命令/args/env 且继承 NewCode 进程 cwd）；不得隐式将 MCP 子进程 cwd 改成 Worktree。MCP tool 的本地路径语义由服务器自身决定，不能宣称受 child Worktree sandbox 约束。若某 MCP server 不能满足边界，由既有 Permission/allowlist 禁用该工具；不得为隔离而更改用户配置语义。

### F7：缓存与根身份隔离

至少 child file-read cache key 必须包含 canonical absolute workspace root、canonical absolute file path、文件 stat signature 和 repository/worktree identity；每个 child cache 独立，不能在主 workspace、另一 Worktree 或另一个 task 间复用。Context artifact 固定落在 child workspace root 下的 `.newcode/context-artifacts/<child-session-id>/` 并由其 ContextManager 清理。

Instructions、Memory、Skill/Agent discovery、Prompt/context 派生缓存若存在，cache key 必须包含其实际 canonical source root/workspace fingerprint；项目级记忆 fingerprint 使用 Worktree root，不因 common Git dir 相同而共享。user-level instructions/notes 可按现有 user scope读取，但自动记忆服务、SessionArchive、父 session、已激活 Skill state、Hook state 与 Context artifacts 不得在 child/main 之间共享。Worktree 目录不含父 `.newcode` 忽略运行数据。

### F8：初始化内容的显式 allowlist

Worktree checkout 默认只包含 Git 跟踪文件。用户级可选配置入口固定为 `~/.newcode/worktree.yaml`，项目请求入口固定为 `<repository-root>/.newcode/worktree.yaml`。user config 为权限上限；project config 只能选择其子集，不能扩权。均为安全 YAML、schema 严格拒绝未知字段，单文件错误产生安全诊断且回退到空初始化 allowlist。

- `copy_files` 与 `copy_ignored_files` 只接受 repository-relative 的精确普通文件路径列表，不接收 glob、绝对路径或目录递归；project 所选路径必须同时出现在 user allowlist 和内建安全路径 allowlist 中。
- 始终拒绝 `.env*`、凭据/token/key、私钥/证书、`.git/**`、`.newcode/{sessions,memory,context-artifacts,skills-state}/**`、MCP headers/tokens、session/archive、cache、日志、临时文件、符号链接/junction 与任何运行时目录；敏感模式 deny 优先于显式 allowlist。
- `dependency_links` 默认空，只接受 user 明确列出的 canonical trusted read-only dependency roots；project 配置不能添加目标。只允许通过平台验证的只读链接/junction，目标必须在可信 allowlist 中且不可由 child 写入；不能证明只读、稳定和不会逃逸 sandbox 时不创建链接，继续运行但依赖不可用。
- Chapter 14 的 hook execution allowlist 固定为空：仓库 `.git/hooks`、用户 hooks path、system/global config 指向的 hooks 均不执行，也不复制到 child。每条 Manager Git argv 显式指定 Manager-owned empty hooks dir，并禁用 system/global config；对 `worktree add` 的 checkout-hook sentinel 必须证明零执行。child Git 仅在 command sandbox 能保证相同 hooks override 且不能由命令覆盖时可用；否则 child Git/`run_command` fail closed。本章不支持需执行 Git hook 的工作流。
- 配置复制、忽略文件复制与 hook 初始化必须在启动 Agent 前完成；逐文件 O_EXCL/原子写，不跟随链接。失败清理本次资源或保留安全标记并失败该 child，不影响主 Agent。

### F9：过期清理

仅清理年龄超过 **30 天** 的资源；mtime 仅作候选筛选，不是删除授权。自动删除要求 tracked/staged/untracked/ignored 文件和目录检查均完整且为空；忽略状态不可查询、扫描不完整、存在未知目录/条目均保留。删除前必须同时通过三层独立门：

1. **路径门**：canonical 目标严格位于 `<repository-root>/.newcode/worktrees/` 内，且逐段无 symlink/junction/reparse point；目标不是 root、main worktree、metadata/owner 目录或 link target。
2. **Git/归属门**：只读 Worktree list 显示唯一注册；common-dir、top-level、branch、marker 与 Manager-owned task ID 全部匹配；不删除无 marker、用户创建或无法证明归属项。
3. **活动/状态门**：跨进程锁确认无活动 task，当前 Manager 无 lease/占用；先清理 manifest 验证过的本任务临时内容，再重新查询 tracked/staged/untracked/ignored 文件与目录、upstream ahead，并完成不跟随链接的 root 遍历；所有状态必须明确为空且扫描完整。无 upstream、Git 不可用、锁竞争、命令输出不完整、扫描错误或状态未知均保留。

每个候选独立失败隔离并产出安全 diagnostic；不能递归清理整个根目录，不能跟随链接删除根外对象，不能调用 `git worktree prune` 替代验证。child 新建 ignored 文件/目录必须导致保留，除非其是 manifest 中明确的本 task 临时项、先按安全删除流程清理且复查后为空。

### F10：子 Agent 结果与用户可见信息

成功/失败结果保持 Chapter 13 固定格式与脱敏上限；Worktree 子任务附加状态 `cleaned|preserved|unavailable`、branch 和相对 Worktree path（必要时安全展示 absolute path），不得附 diff、文件清单、秘密、完整 Git 输出、stack trace。完成通知只由主 AgentLoop 安全点按 parent session/generation 投递一次；worktree 内容不写入父 SessionArchive，除 AgentLoop 原有安全结果摘要外不持久化 task state。

## 错误模型

对模型与日志暴露稳定、短小、脱敏的错误码，至少包括：`worktree_repo_unavailable`、`worktree_path_invalid`、`worktree_path_conflict`、`worktree_branch_conflict`、`worktree_git_failed`、`worktree_git_timeout`、`worktree_registration_invalid`、`worktree_owner_invalid`、`worktree_busy`、`worktree_setup_failed`、`worktree_command_sandbox_unavailable`、`worktree_state_unknown`、`worktree_preserved_changes`、`worktree_cleanup_denied`、`worktree_cleanup_failed`。Git stderr、路径中的用户名、命令环境、diff、hook 输出与异常堆栈不得直接暴露；诊断仅记录错误码和允许的非敏感阶段信息。

创建或安全验证失败时不启动 Worktree child、不回退 shared、不改其他任务。完成清理失败时保留目录并报告安全错误，不影响主 Agent、其他 child 或 CLI cleanup。策略拒绝优先于文件副作用。

## 非目标

- Worktree 间 merge、cherry-pick 回主分支、同步/自动提交或推送。
- Worktree 跨进程/跨会话恢复、跨会话 task 持久化、后台服务守护。
- Agent Teams、多个 child 的 Git 协作、Worktree 分支 UI/命令。
- Git 任意命令代理、远端访问、自动 fetch/push、自动解决冲突。
- 将 Git Worktree 视为通用 OS sandbox；无可验证共享 Git metadata 与 process confinement 时 child Git/`run_command` fail closed。
- 改写 MCP stdio cwd 语义、Provider orchestration、Permission policy、主 AgentLoop 的 gates、Chapter 13 tool policy/预算/取消/通知规则。
- 默认复制 ignored 文件、秘密、session、Memory、Context artifact 或任意 Git hooks。

## 测试范围与验收标准

所有测试使用临时 home/workspace 和本地临时 Git repository；禁止网络 remote、push、生产 secret、真实 MCP 与生产 Git hooks。使用 fake Git executable/clock/lock/command sandbox fixture 验证失败、timeout 与平台行为。平台不支持 junction/symlink 创建时仅 skip 对应创建 fixture；拒绝路径、错误码和 fail-closed 行为仍必须在所有平台测试。

必须覆盖：

1. schema 缺省 `shared`、合法 `worktree`、非法值隔离和既有 shared 回归。
2. 路径各非法段、层级/长度边界、branch ref validation、canonical containment、盘符/UNC/绝对路径、symlink/junction/root escape 与目标冲突。
3. 临时 repo 创建与注册检查、验证后启动、已有目录不能凭存在恢复、同 task/branch 占用冲突、common-dir/branch/HEAD/marker 不匹配拒绝、argv 无 shell/注入。
4. 全六工具 cwd；文件工具相对路径 escape 被拒绝；`run_command` 的真实 cwd 及进程 sandbox 有效时的 escape 阻断；无 sandbox 时零命令执行。MCP stdio cwd 保持原配置语义。
5. 主 workspace、不同 worktree/task 的 read cache 与 instructions/Memory/Context 派生源隔离；Memory project fingerprint 与 child root 对应。
6. 配置 allowlist 合并和密钥/运行时排除；dependency link 目标 allowlist、写权限和 escape 拒绝；默认不链接。所有 Manager Git command hooks sentinel 零执行。
7. 清洁 Worktree 可清理；未提交 tracked/staged/untracked/ignored 文件/目录、领先 upstream commit、无 upstream、离线/查询失败/扫描不完整、注册未知均保留；child 创建 ignored 文件的回归证明不自动删除；用户目录/main/root 外目标绝不删除。
8. 三层过期过滤、活动锁、年龄边界、cleanup race、失败隔离；cancel/timeout/provider exception/CLI 所有退出路径中的有限资源清理。
9. Worktree completion result 脱敏、安全路径/branch、通知只投递给正确父 session/generation 一次；child state 不进入父 Archive。
10. Git 命令矩阵验证只读 allowlist 可用；`git add/commit/update-ref`、ref/config/worktree 写入和全部未列出命令在启动子进程前拒绝，越权尝试对 objects、refs、index、HEAD、main/其他 Worktree admin metadata 均零副作用。
11. 当前 Worktree admin dir（含 index/HEAD）与 common Git metadata 对 child 只读；若命令解析、ACL 或 hooks 隔离无法证明，Git 与 `run_command` 零子进程调用。
12. Chapter 4–13 Provider、AgentLoop、工具、Permission、Plan/Do、Context、Memory、MCP、Skill、Hook、Commands、CLI 与完整 pytest 回归。

验收时，六个内置工具仍经既有安全链；shared Agent 行为不变；Worktree child 绝不访问 main workspace 文件。`run_command` 若没有通过平台 sandbox 验证，明确不可用而不是假装隔离。所有 Git 命令在临时本地仓库运行，无远端与全局 Git 配置变更。

## 架构边界与数据流

```text
Definition(isolation=worktree)
  -> SubAgentManager task lease / fixed policy snapshot
  -> WorktreeManager: validate repo/path/branch -> safe git argv -> verify registration
  -> optional exact allowlisted setup -> child ToolContext(root=cwd=worktree)
  -> existing child AgentLoop -> Permission -> ToolScheduler -> executor
  -> child result + Worktree cleaned/preserved metadata
  -> Manager bounded result queue -> parent AgentLoop safe-point notification
```

WorktreeManager owns Git lifecycle and ownership metadata. Child AgentLoop continues to own independent messages, Permission, budget, cancellation, Context and read cache. ToolContext owns immutable root/cwd identity. CLI owns manager lifetime and invokes cleanup without blocking existing Hook → Memory → Context → MCP cleanup. Provider remains unaware of Worktree. No worker mutates the parent ChatSession.

## 完成定义

四个 Chapter 14 文档审核通过后，实施各 Phase。每 Phase compileall 与 targeted pytest 通过才可继续；最终 targeted regression、full pytest、静态边界审计、`git diff --check` 和 fake CLI/local Git acceptance 全部通过。未验证的安全能力按 fail-closed 处理并列为未完成，不以文档承诺替代测试证据。
