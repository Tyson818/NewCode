# Chapter 15：Agent Teams —— 实施计划

## 架构概览与边界

当前 Provider / `AgentLoop.run()` 为同步迭代接口，因此轻量 backend 是有界 thread worker，不是 asyncio 改造。Teams 自己拥有持久 `TeamStore`、任务/邮箱/审批状态与成员恢复语义；Chapter 13 `SubAgentManager` 仍是一次性任务委派，不承担团队持久化。Provider 不导入 Team。

```text
CLI / static /team role attach
  -> TeamRuntime (one active CLI scope, bounded workers, backend capabilities)
      -> TeamStore (user .newcode/teams, lock + CAS + atomic records)
      -> TeamTaskService / Mailbox / ApprovalService
      -> TeamMemberRunner -> existing AgentLoop -> Permission -> ToolScheduler -> executor
      -> WorktreeManager team-scoped lease restore
      -> TeamIntegrationManager (Permission-approved fixed Git transaction)
```

单向依赖：`teams.types/paths` → `teams.config/store` → `tasks/mailbox/approvals` → `backends/runtime/member` → `tools/integration` → CLI/AgentLoop adapters。AgentLoop 只依赖固定 Team tools / runtime port；Provider 保持原接口。外部 pane worker 只通过受控 TeamStore 与当前用户 ACL 的内存 IPC 获得运行期配置；不写 Team secrets。Chapter12 Hook subagent placeholder 不连接 TeamRuntime。

## 核心模型与接口

- `TeamManifest`: schema version、team ID/name、workspace fingerprint、lead member ID、revision、backend choice、roster、creation/update times；没有 credential/provider/permission-session object。
- `TeamMemberRecord`: member ID/name、Definition name、role、相对Worktree path/lease identity、启动base commit B、backend、requires-plan-approval、state、auto_wake、checkpoint session ID、plan revision与usage totals。pause/stop关闭auto_wake，显式resume校验后才重开；运行句柄只在runtime memory/OS lock中。
- `TeamTask`: ID/title/description/creator/assignee/status/dependency IDs/revision/timestamps/result summary；所有更新使用 `expected_revision`。
- `TeamMessage`: immutable envelope + sequence + idempotency key + redacted body/protocol; runtime-injected sender; per-recipient ack ledger。
- `TeamPlan`: ID/member/revision/content/status/decision identity/timestamp/associated message ID。
- `TeamStore`: `create_team`, `load_team`, `commit_transaction`, `recover_projections`, `list_messages`, `ack_message`, `acquire_team_lock`, `close_scope`。单个不可变提交记录是业务提交点，包含after-images、outbox、idempotency、sequence及完整广播清单；其它JSON是投影。所有路径先通过 root verifier；CAS/锁不通过无写入。
- `TeamBackend` protocol: `probe()`, `start(member, runtime_bootstrap)`, `wake(member_id)`, `request_stop(member_id)`, `join_until(deadline)`, `close()`。probe只做无副作用能力检查；start创建IPC/pane后完成身份/唯一lease握手，失败只清理本次资源；选择后无 fallback。
- `TeamMemberRunner`: 接受 immutable `TeamMemberLaunch`、恢复安全 ChatSession、运行一轮同步 AgentLoop、checkpoint 后发布 `idle`/failure message；不持有 Lead session reference。
- `TeamIntegrationManager`: `preview(target_member_id, expected_member_revision)` 与固定integrate契约；记录B、实际target branch T/M、frozen digest、S/I及阶段。先B上生成S，再在M上三方合并成I，最后在目标cwd执行ff-only并复核branch/index/worktree；内部固定argv，不暴露Git通用接口，不承诺跨文件系统原子事务。
- `TeamPolicySnapshot`: 分离不可变启动上限P0、当前只收窄Pt与业务审批At；每轮和executor前在版本屏障内重算。批准只能解除At限制，不改变P0/Pt/Permission。pane通过认证IPC接收快照，不读取Lead可变状态；失联禁止新工具。所有member confirmer非交互deny-by-default。
- Coordinator直接可见集合与用户已授权的Team委派上限单独投影；member的P0启动即固定，仍受父mode/Skill/Pt和独立Permission制约，不因批准扩大。Coordinator/成员MCP禁用时不创建client或启动discovery，Hook队列同样受限，不能用启动过程绕过工具禁用。
- `TeamCheckpoint`: Chapter9兼容完整JSONL与同发布单元的processed-message-ID元数据；ack必须验证收件人/有效checkpoint/session generation，不以独立receipt代替交付证明。

## 持久化与事务

`~/.newcode/teams/<generated-team-id>/` 下放 `transactions/`、Team/task/message/ack投影、每member checkpoint、`integrations/`和`locks/`。持owner OS锁完成校验/CAS，生成含业务变更与全部outbox的单记录事务，fsync后排他发布并保证目录项持久化；只有该commit是事实来源，然后重建投影并wake。提交前崩溃无业务成功，提交后崩溃重放同ID/sequence/after-images，不重复业务变更；提交链损坏fail closed，投影缺失可重建。单文件atomic replace不代表多文件事务。事务上限512KiB，Team日志8,192条/64MiB触顶拒绝，不在线压缩。锁owner process identity+nonce；不根据mtime回收。

Mailbox read不ack；收件人有效checkpoint记录ID后才能提交ack事务。自动投递先checkpoint后ack，模型read后由runner在下次主请求前checkpoint观察；checkpoint-ack间崩溃只补ack。系统plan/task/member协议由可信服务把业务状态及outbox写在同一事务，模型只能发送普通message kind。投递去重不承诺LLM或外部工具exactly-once。

TeamStore恢复校验 version/schema、workspace fingerprint、目录和每个普通文件的 canonical containment。非法单条 mailbox/task 可隔离为固定 diagnostic，不阻断只读其它条目；Team manifest/lock 状态损坏则不启 worker、不覆盖数据。Member messages 调 Chapter9 `SessionArchive` 兼容 redaction、tool pair truncation 与 workspace fingerprint；sessions root 位于 Team 专用 member 目录，不写入主 SessionArchive。

容量为 spec 固定值；队列满、邮箱/记录满时拒绝操作，未确认消息不可淘汰。归档/删除 Team 资料不自动发生；`archive` 只关闭，不清理内容。

## 模块与文件组织

```text
newcode/teams/__init__.py          对外安全类型与 API
newcode/teams/types.py             IDs、manifest/member/task/message/plan/status/errors
newcode/teams/paths.py             user root、name/ID、reparse/containment/ACL
newcode/teams/config.py            user/project strict YAML 与 coordinator 双重 gate
newcode/teams/store.py             atomic JSON records、workspace fingerprint、locks/CAS
newcode/teams/tasks.py             bounded task service、DAG、state machine
newcode/teams/mailbox.py           envelope、broadcast、idempotency、ack/replay
newcode/teams/approvals.py         plan revisions、structured approval state
newcode/teams/backends.py          protocol、capability probe、backend precedence
newcode/teams/runtime.py           process Team scope、bounded queue/workers/shutdown
newcode/teams/member.py            checkpoint、idle/resume、Member AgentLoop adapter
newcode/teams/tools.py             固定 role tools/schema 与 Team runtime port
newcode/teams/coordinator.py       read-only Lead policy + gated integration tool policy
newcode/teams/integration.py       controlled Git snapshot/commit/merge/verify/rollback
newcode/teams/worker.py            pane process entry、OS identity-checked ephemeral IPC
newcode/worktrees/{manager,git,types}.py 仅增加 Team 专用 resume lease/manager write capability
newcode/agent/loop.py              注册 Team tools、每轮 policy refresh，不改 Provider
newcode/agent/mode.py              role-specific visibility 与 Plan visibility
newcode/tools/registry.py          tools 分类/静态冲突保护（如确有需要）
newcode/commands/builtins.py       静态 /team local command
newcode/cli.py                     Team config、runtime/role/backend/cleanup 生命周期
newcode/permissions/{manager,normalizer,builtins}.py 受控 Team operation risk，不放宽通用规则
tests/test_teams_*.py              类型、路径、存储、任务、消息、审批、后端、成员、策略、集成
tests/test_agent_loop_teams.py     AgentLoop/ToolScheduler/Permission gates
tests/test_cli_teams.py            static command、fake CLI 生命周期
tests/test_teams_worktree_resume.py Chapter14 Team lease restore boundary
```

不新增Python依赖，不改Provider。内部入口统一 `newcode/teams/worker.py` / `python -m newcode.teams.worker`，实际可信已安装解释器启动使用`-I -B`并验证module origin，bootstrap cwd不取成员Worktree。tmux需支持多command参数直接exec，禁止内层shell-string/send-keys；外层shell=False不足以证明。实际模块自检/argv完整性/特殊字符零注入与真实pane启动分别取证。

## Phase 划分与退出门

### Phase 1：纯模型、路径和安全持久化（T1–T4）

**目标：** 固定领域 schema、workspace-scoped user Team root、配置校验/合并、atomic store 与安全 member checkpoint adapter。

**依赖：** 无。根据现有 SessionArchive/Memory fingerprint/Worktree path patterns 实现。

**文件：** `newcode/teams/{__init__,types,paths,config,store,member}.py`；新增 `tests/test_teams_types.py`、`test_teams_paths.py`、`test_teams_store.py`、`test_teams_checkpoints.py`。

**验证：** IDs/schema/status、symlink/junction/reparse、ACL fail-closed、workspace mismatch、同名文件冲突、提交日志各步crash/replay/CAS/lock、有效checkpoint与processed-ID同发布、redaction/tool-pair restore。此阶段不启动Agent/backend。

**安全门：** 若用户目录 owner/ACL/锁/路径安全不能证明，不写 Team 数据，停止后续阶段。

### Phase 2：共享任务、邮箱和计划审批（T5–T8）

**目标：** 实现 CAS task store、DAG 依赖门、身份绑定邮箱/确认重放、plan revision approval。

**依赖：** Phase 1。

**文件：** `teams/{tasks,mailbox,approvals}.py`、`types.py/store.py` 必要增量；新增 `tests/test_teams_tasks.py`、`test_teams_mailbox.py`、`test_teams_approvals.py`。

**验证：** 锁竞争、并发claim/version conflict、环检测/缺失依赖、容量；事务提交/幂等/sequence/全广播清单/投影每步崩溃恢复；read不ack、他人/未checkpoint ack拒绝；系统协议伪造拒绝，structured approval revision；At不是AgentMode.PLAN。

**安全门：** 用户文本、模型 sender 字段和普通“批准”字样都不能改变身份/approval state；锁不确定则不写。

### Phase 3：Backend capability 与启动/停止（T9–T11）

**目标：** 固定 backend precedence/capability; lightweight bounded synchronous worker; pane tmux 子进程与无 secret IPC handshake。

**依赖：** Phase 1–2。

**文件：** `teams/{backends,runtime,worker}.py`、必要时 `types.py`；新增 `tests/test_teams_backends.py`。

**验证：** fake backend并发/queue/timeout/shutdown；实际Python模块--check启动、argv原样/特殊字符零注入、无secret；无副作用probe和有资源startup分离、错误peer/重复executor/每步启动失败资源清理；真实tmux内层direct-exec+pane/process/IPC另列实测门，mock不能替代。

**安全门：** 当前环境未检测 tmux；若真实 pane 无法实测只可标未验证，不妨碍 lightweight 后续验收。若 pane 无安全 IPC，必须保持 unavailable，不能写 secret 到 argv/env/磁盘。

### Phase 4：Team Member AgentLoop、idle/checkpoint/recovery（T12–T15）

**目标：** 在现有同步 AgentLoop 中运行成员；独立 session/permission/context/Skill/usage；final checkpoint→idle；runtime 与跨 CLI restart 恢复；Team-scoped Worktree lease restore。

**依赖：** Phase 1–3。

**文件：** `teams/{member,runtime}.py`、`subagents/policy.py` 仅复用策略接口；`worktrees/{manager,git,types}.py` Team-only API；新增 `tests/test_teams_members.py`、`test_teams_worktree_resume.py`。

**验证：** natural final/exception/cancel/provider error；独立消息/Context artifact/permission/cache/skill；pairing/checkpoint/restore/fingerprint/budget；require_confirmation非交互零执行、不继承Lead allow；启动B持久化、Worktree marker/registration/lock不符拒绝；成员MCP及Hook shell/HTTP无可验证隔离时禁用，实例独立不能当隔离证明。

**安全门：** Worker 不能读写 Lead ChatSession。任何 lease owner 不确定均不启 child、不回退 shared。

### Phase 5：角色工具、AgentLoop 与 `/team` CLI role attach（T16–T19）

**目标：** 静态 Team tools、固定 role visibility、Plan/Do/Permission/Scheduler gate、CLI session Team scope 与 coordinator 双重开关。

**依赖：** Phase 1–4。

**文件：** `teams/{tools,coordinator,runtime,member}.py`；`agent/{loop,mode}.py`、必要时 `tools/registry.py`、`permissions/{manager,normalizer,builtins}.py`、`commands/builtins.py`、`cli.py`；新增 `tests/test_teams_tools.py`、`test_teams_policy.py`、`tests/test_agent_loop_teams.py`、`tests/test_cli_teams.py`。

**验证：** spec五工具每operation的角色/全部参数组合/禁止字段/合法状态/读写/返回/错误矩阵；身份只能runtime注入；DO+awaiting_approval仅协调写、PLAN不放宽；P0/Pt/At隔离、计划修订及父mode/Skill收窄后的旧响应重验；非交互confirmation零执行。CLI命令/clear/resume兼容。

**安全门：** Permission/Plan/Skill/MCP/ToolScheduler 任何 gate 都不得绕过；coordinator 不显示文件写入/原始 shell，且项目配置不可打开。

### Phase 6：安全集成与 coordinator 固定能力（T20–T22）

**目标：** 固定B→冻结快照→S、目标实际分支T/M→三方合并I→目标cwd ff-only同步branch/index/worktree；阶段日志与崩溃恢复；冲突/并发/unknown保留成果；coordinator封闭允许集合。不声称Git与文件系统整体原子。

**依赖：** Phase 4 Worktree resume 和 Phase 5 tools/Permission 已完成。

**文件：** `teams/integration.py`、`teams/coordinator.py`、`worktrees/{git,manager,types}.py`；`cli.py`仅注入绑定integration ID/B/T/M/digest的用户单写者确认端口（Permission allow与模型confirmation都不代替它）；新增 `tests/test_teams_integration.py` 与 coordinator policy 扩展。

**验证：** 本地非main目标分支；两个member同B、目标已含另一member提交仍保留双方非冲突成果；staged+unstaged/新增/删除/二进制/超限拒绝；真实in-flight工具退出屏障；dirty/ignored/lock/secret；外部HEAD/index/文件并发变化；每阶段/Git前后崩溃、S/I识别幂等、applying_target恢复/unknown；目标branch/index/working-tree完全同步；hooks零执行，child零Git写。

**安全门：** 仅 TeamIntegrationManager 的 Permission-allowed explicit action 可写 Git；不能证明恢复/回滚边界则保留临时与 Member 数据并停止。

### Phase 7：CLI 生命周期、回归与最终验收（T23–T26）

**目标：** Team 与 session/process lifecycle 清理；Chapter 4–15 regression；静态边界审核和 fake CLI/local Git E2E；可选真实 pane acceptance。

**依赖：** Phase 1–6。

**文件：** `cli.py`、`teams/runtime.py`、必要的 `tests/test_teams_lifecycle.py` 与既有回归测试；默认不改 Provider/Permission 核心。

**验证：** clear/resume/EOF/exit/KeyboardInterrupt/startup/runtime exception、checkpoint/stop 竞态，Hook→Memory→Context→Worktree→MCP failure isolation；全量 compileall/pytest/diff check。pane backend 的真实 tmux 进程验收独立报告，缺少 tmux 时留未验证项。

**最终安全门：** 主回复/普通 SubAgent兼容；无 Provider→Teams import；Team tool 全过原有 gates；child shell/git 写仍 fail closed；无 secrets/permission grants 持久化；所有 skip 有原因；不访问网络、真实第三方 MCP、生产 hook/secret，不改全局 Git config。

## 关键风险与缓解

| 风险 | 缓解 |
|---|---|
| Chapter14 仅进程内 Worktree lease 与 Team 跨进程恢复冲突 | 只加验证 Team owner marker 的 Team-specific lease claim；不开放通用 resume，必须新集成测试证明 registry/lock/owner；unknown 不复用。 |
| AgentLoop 同步且成员长期 idle | bounded worker thread 执行同步 AgentLoop；idle 等 mailbox 不发请求；超时/取消从现有 cancel/provider timeout 边界退出，不将 async SDK 混入 Provider。 |
| tmux pane 启动时传递 API/MCP secret | 命令行/env/磁盘都不传 credential；user-ACL local IPC 内存 bootstrap；无法证明 IPC 就 backend unavailable；显式 pane 不 fallback。 |
| Manager Git integration可能覆盖外部编辑 | 用户确认单写者前提；Manager锁仅约束自身；B与T/M分离、冻结S/三方I；目标前后HEAD/index/内容复核，外部变化即停止/unknown，不承诺整体原子、不自动回滚目标。 |
| mailbox/task多记录crash/race | OS锁+单commit日志含业务after-image/outbox/幂等/全广播；投影可重建；checkpoint后才ack，不声称外部工具exactly-once。 |
| coordinator经其它入口恢复写能力 | 封闭允许集合只有读取+固定Team工具；ordinary agent/Skill runner/MCP/Hook shell/HTTP默认禁用；委派和集成仍需原有Permission链。 |

## 每 Phase 验证约定

每个 Phase 先运行：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest <本 Phase 已创建的 targeted tests> -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter15-phaseN"
git -c safe.directory=F:/agent/Newcode diff --check
```

测试在各 Phase 首个任务中先创建，再被该 Phase 后续任务引用。Phase gate 不得引用尚未创建的文件；失败立即停在本 Phase。最终 Chapter4–15 targeted 与全量 pytest 使用 `--basetemp "$env:TEMP\newcode-pytest-chapter15-final"`。全部 provider/backend/mailbox/git acceptance 使用 fake/local fixture；真实 pane 后端只有目标环境实际执行 `tmux` +独立 worker process/IPC 后才能标记通过。
