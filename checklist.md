# Chapter 15：Agent Teams —— 验收清单

> 按 Phase 1→7 顺序逐项勾选。每个 targeted test 文件必须已由对应 Phase 的较早 Task 创建；每 Phase 退出门全过才可继续。所有 basetemp 位于 `$env:TEMP`。

## 全章安全前置

- [ ] 测试只使用 fake Provider/backend、临时 home/workspace、临时本地 Git repo 和 fake/local IPC；无 remote/push、生产 secret、第三方 MCP、生产 hooks、全局 Git 配置修改。
- [ ] Provider 不导入 Team；AgentLoop 仍经 mode/visibility → Permission → ToolScheduler → executor；Team tools 不可绕过 Skill/MCP/Context/Memory/Hook/Command 现有边界。
- [ ] 普通 CLI/普通 SubAgent 没有 Team collaboration tools；Team Member 没有 Lead-only spawn/approve/integrate；固定 schema 不按成员动态生成。
- [ ] 未设置 user Teams enable 时功能关闭；project config 不能 enable；coordinator 同时需要 user config 与 `NEWCODE_TEAM_COORDINATOR=1`。
- [ ] Chapter12 Hook subagent placeholder 仍 unavailable；Team 不会由 Hook 自动创建或递归调度。
- [ ] pane 后端必须有真实 tmux pane + 独立 process + IPC 验收证据；fake backend/mock 不得标成真实运行通过。

## Phase 1：模型、路径与持久化（T1–T4）

- [ ] **T1** 类型/错误/状态/资源固定；P0启动上限、Pt只收窄快照、At审批门独立；事务commit/投影/delivery proof JSON-safe，无secret/权限运行对象。
- [ ] **T2** Team store root/name/config校验；workspace binding/reparse containment有效；项目配置只收窄；默认 Teams disabled。
- [ ] **T3** owner锁/CAS内单提交记录含业务after-image/outbox/幂等/sequence/完整广播；fsync及目录持久化后才投影/wake；每步crash/replay无静默丢信/重复业务变更；日志损坏拒绝、投影可重建；不靠mtime回收锁。
- [ ] **T4** Chapter9脱敏/配对/指纹/预算正确；processed-ID与有效checkpoint同发布单元，Context压缩不丢去重元数据；他人/旧generation/截断观察不能证明交付，权限确认不持久化。

Phase 1 命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_teams_types.py tests/test_teams_paths.py tests/test_teams_store.py tests/test_teams_checkpoints.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter15-phase1"
git -c safe.directory=F:/agent/Newcode diff --check
```

**Phase 1 退出门：** 四项通过；路径、ACL 或跨进程锁不确定时不得进入持久 Team write。

## Phase 2：任务、邮箱、审批（T5–T8）

- [ ] **T5** Task CRUD/版本/CAS/状态机正确；重复 claim 幂等、冲突不覆盖。
- [ ] **T6** DAG 拒绝 missing/self/cycle；依赖未完成不可 claim/执行；delete限制有效。
- [ ] **T7** read不ack；ack校验本人/有效checkpoint ID/generation；同键异正文拒绝；message ID/幂等/sequence/广播清单/投影/checkpoint/ack每中间步crash验证；系统kind不可伪造；投递去重不宣称LLM/外部副作用exactly-once。
- [ ] **T8** 仅可信服务提交计划/审批业务+outbox同事务；actor来自runtime，文字“同意”无效；修订使At批准失效，不改变P0/Pt/Permission；DO待批协调写与PLAN不写分开测试。

Phase 2 命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_teams_types.py tests/test_teams_store.py tests/test_teams_tasks.py tests/test_teams_mailbox.py tests/test_teams_approvals.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter15-phase2"
git -c safe.directory=F:/agent/Newcode diff --check
```

**Phase 2 退出门：** task/mailbox/approval race及每提交窗口恢复全过；多记录提交不能仅凭atomic replace宣称一致；owner/lock不明时零覆盖。

## Phase 3：运行后端（T9–T11）

- [ ] **T9** backend precedence稳定；probe仅版本/模块--check/能力，无pane/socket创建；start的资源验证单独列出；显式不可用不fallback。
- [ ] **T10** synchronous AgentLoop 在有界 worker内执行；同一成员串行；idle不发请求；取消和shutdown共享1秒总等待。
- [ ] **T11** 唯一`newcode/teams/worker.py` / `python -m newcode.teams.worker`实际模块可启动；tmux内层多参数direct-exec、参数原样/特殊字符零注入有真实证据，非仅外层shell=False。错误peer/重复executor拒绝，IPC/pane/握手/lease各步失败只清本次资源，secret只走私有IPC。

Phase 3 命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_teams_backends.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter15-phase3"
git -c safe.directory=F:/agent/Newcode diff --check
```

**Phase 3 退出门：** lightweight独立通过；模块自检/假后端与真实pane证据分别记录；无tmux则pane unavailable/未验证，不阻塞lightweight、不标两后端均已验收、不自动安装。

## Phase 4：成员执行、恢复、Worktree lease（T12–T15）

- [ ] **T12** member独立状态/权限且不继承Lead allow；require_confirmation非交互拒绝、executor及主confirmer零调用；存启动B。MCP/Hook实例独立不算隔离，无目录隔离证明则成员MCP/Hook shell/HTTP禁用，stdio cwd不隐式更改。
- [ ] **T13** natural final先checkpoint再事务发布idle/outbox；read后checkpoint不自动ack、crash恢复去重；pause/stop关闭auto_wake直到显式resume；in-flight工具真实退出后才给静止证明，idle/cancel标记不足；异常保留旧checkpoint。
- [ ] **T14** CLI重启恢复做 workspace/pairing/budget校验并重建permission；不恢复session allow/provider等runtime object。
- [ ] **T15** Team-specific lease resume逐项验证owner/marker/path/branch/HEAD/common-dir/唯一registration/锁；unknown时不启动、不回退shared。

Phase 4 命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_teams_checkpoints.py tests/test_teams_members.py tests/test_teams_worktree_resume.py tests/test_subagents_worktrees.py tests/test_worktrees_manager.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter15-phase4"
git -c safe.directory=F:/agent/Newcode diff --check
```

**Phase 4 退出门：** 不继承临时授权，Worktree owner不明不复用；旧父/新父session不串状态。

## Phase 5：工具、权限与 CLI role attach（T16–T19）

- [ ] **T16** spec五工具每operation的角色、必填/可选/禁止字段、状态、读写、结果/错误完整矩阵测试；拒绝伪造身份/其它operation字段；target不是actor；普通会话/SubAgent零Team工具。
- [ ] **T17** 每请求与executor前重算P0∩Pt∩At及全部gate；旧响应在修订/失批/Do→Plan/Skill收窄后零执行；DO待批只准明确协调写、PLAN仅status；成员require_confirmation拒绝、主confirmer零调用；批准不生成grant/恢复收窄能力。
- [ ] **T18** `/team lead/open/status/close` 静态且workspace-bound；`/clear`/`/resume` detach，不把旧消息注入新session；无Team时CLI行为不变。
- [ ] **T19** user config AND env开启coordinator，project不可开启；直接allowlist只准读取+五Team工具/本地命令；委派P0独立且不扩权，仍受父mode/Skill/Pt/Permission。ordinary agent/Skill runner/MCP discovery/Hook shell/HTTP及未知入口全拒绝；旧队列/在途动作重验、静止后才进入。

Phase 5 命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_teams_tools.py tests/test_teams_policy.py tests/test_agent_loop_teams.py tests/test_cli_teams.py tests/test_agent_loop.py tests/test_agent_loop_permissions.py tests/test_agent_scheduler.py tests/test_cli_commands.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter15-phase5"
git -c safe.directory=F:/agent/Newcode diff --check
```

**Phase 5 退出门：** 无 Team 权限旁路；Plan mode无 mutation tool；普通CLI/命令/Skill/MCP语义通过回归。

## Phase 6：受控代码集成（T20–T22）

- [ ] **T20** 存B、解析实际目标T/M；executor lease/屏障证明全部工具静止，不凭idle/cancel。单写者用户确认；外部编辑不受Manager锁保护。冻结B相对manifest/blobs含staged+unstaged/新增/删除/二进制，大小/secret/link/ignored冲突拒绝；preview只读、digest不符拒绝。
- [ ] **T21** B上快照commit S、M上三方merge成I、目标cwd ff-only同步branch/index/worktree并复核；两成员同B与目标已有第一成员提交测试；冲突不选边。每阶段crash/超时、幂等识别S/I、applying_target只读恢复、外部HEAD/index/文件变化和unknown保留全部成果；hooks零执行，无reset/clean/force/删分支，不声称整体原子。
- [ ] **T22** coordinator只走同一固定integration API；所有其它副作用入口无旁路；Permission拒绝零Git写，单写者/静止/阶段恢复门同样生效。

Phase 6 命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_teams_integration.py tests/test_teams_policy.py tests/test_worktrees_git.py tests/test_worktrees_manager.py tests/test_worktrees_command_sandbox.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter15-phase6"
git -c safe.directory=F:/agent/Newcode diff --check
```

**Phase 6 退出门：** 非main目标与branch/index/worktree全同步、两成员同B/删除/二进制/外部并发/每阶段崩溃测试全过；dirty/真实活动/锁/冲突/unknown均保留；目标没有自动回滚，临时abort也需验证归属。

## Phase 7：生命周期、全量回归与验收（T23–T26）

- [ ] **T23** EOF/exit/KeyboardInterrupt/异常/startup failure/clear/resume均有限清理；team failure不阻断SubAgent→Hook→Memory→Context→Worktree→MCP；单一1秒总等待；双重关闭幂等。
- [ ] **T24** Chapter4–15 targeted与静态审计覆盖全部operation契约、P0/Pt/At/confirmation、邮箱多记录崩溃一致性、coordinator所有入口、B/S/M/I及外部并发局限、pane真实证据；Provider无Team导入，普通会话既有能力兼容，Hook placeholder继续unavailable。
- [x] **T25** compileall与全量pytest通过；所有skip写明具体平台/fixture原因；`git diff --check`通过。
- [x] **T26** fake CLI/local Git 验收由既有 CLI、审批/邮箱/恢复、Lead/coordinator 集成用例组合覆盖 create→spawn→task/mailbox/approval→idle/wake/reopen→preview/integrate/cleanup。pane仅真实tmux+process+IPC运行后标通过，否则标未验证。

Chapter15 targeted regression（只引用前序 Phase 已创建的 Team tests）：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_teams_types.py tests/test_teams_paths.py tests/test_teams_store.py tests/test_teams_checkpoints.py tests/test_teams_tasks.py tests/test_teams_mailbox.py tests/test_teams_approvals.py tests/test_teams_backends.py tests/test_teams_members.py tests/test_teams_worktree_resume.py tests/test_teams_tools.py tests/test_teams_policy.py tests/test_teams_integration.py tests/test_teams_lifecycle.py tests/test_agent_loop_teams.py tests/test_cli_teams.py tests/test_agent_loop_subagents.py tests/test_cli_subagents.py tests/test_worktrees_manager.py tests/test_worktrees_git.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter15-regression"
```

全量验收：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter15-final"
git -c safe.directory=F:/agent/Newcode diff --check
```

**最终退出门：** targeted+full pytest、静态审计、本地 Git/CLI acceptance完成；任何真实 pane、symlink/reparse、OS ACL、IPC或锁能力未运行必须明示未验证，不能标为通过。不得暂存、提交或push作为验收步骤。

### Chapter 15 最终执行记录（T25–T26）

- `.venv\Scripts\python.exe -m compileall newcode`：通过。
- T26 复用专项：`test_cli_real_member_agentloop_through_fake_factory`、结构化计划审批、mail/wake/checkpoint 去重、跨 generation 恢复、pause/resume owner、`test_t21_real_tool_permission_manager_and_local_git`（Lead/coordinator）、双成员同基准集成；**9 passed，491.34s**。覆盖分布在既有用例中，并非单个串联脚本；未重复建立一套 E2E 测试。
- 全量：`.venv\Scripts\python.exe -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter15-final"`：**1181 passed, 13 skipped, 0 failed，2828.13s**。
- 13 个 skip：Context artifact、Permission sandbox、tools workspace 的链接 fixture（Windows `WinError 1314`）；Hook loader、instructions、Memory、session persistence、skills、subagents、worktrees 的符号链接 fixture（当前环境 OSError/不支持）；Team backend 真实 pane（未安装 tmux）。symlink/reparse、真实 pane 均未据此记为通过。
- `git -c safe.directory=F:/agent/Newcode diff --check`：通过。Windows、当前用户权限条件下执行；非 Windows 与真实断电恢复未验证。轻量团队路径验收通过，不代表 pane 等所有后端均验收通过。
