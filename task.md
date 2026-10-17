# Chapter 15：Agent Teams —— 原子任务

## 执行约定与范围

- 仅在 Chapter 15 四份文档获批后实施。Phase 顺序 Phase 1 → 7；每 Phase 的 compileall、targeted pytest、`git diff --check` 全过才继续。
- 测试默认 fake Provider/backend、临时 home/workspace、本地 Git repo、fake/local IPC；不访问 remote、不 push、不使用生产 secret、第三方 MCP、生产 Hook，不改全局 Git 配置。
- 当前 Provider 与 AgentLoop 均同步；不得为了协程重构 Provider。Team 不改变 Chapter13 SubAgent 语义；Hook `subagent` action 保持 unavailable。
- 单任务允许文件为最大范围，不允许顺带改其他模块。跨 Phase 文件变更必须由具体 Task 明确列出。新增依赖默认禁止。
- 测试文件先在同 Phase 的第一个相关 Task 中创建；后续任务才可引用。当前环境无 tmux；真实 pane 测试如果仍无可用环境只能标未验证，不能以 fake mock 代替。

## 文件目录

| 功能 | 允许新增/修改文件 |
|---|---|
| Team 数据/路径/配置/存储 | `newcode/teams/__init__.py`、`types.py`、`paths.py`、`config.py`、`store.py`、`tasks.py`、`mailbox.py`、`approvals.py` |
| 后端/成员/工具/集成 | `newcode/teams/backends.py`、`runtime.py`、`member.py`、`tools.py`、`coordinator.py`、`integration.py`、`worker.py` |
| 既有系统集成 | `newcode/agent/loop.py`、`mode.py`、必要时 `newcode/tools/registry.py`、`newcode/commands/builtins.py`、`newcode/cli.py`、`newcode/permissions/manager.py`、`normalizer.py`、`builtins.py`、`newcode/worktrees/manager.py`、`git.py`、`types.py` |
| Team 单测 | `tests/test_teams_types.py`、`test_teams_paths.py`、`test_teams_store.py`、`test_teams_checkpoints.py`、`test_teams_tasks.py`、`test_teams_mailbox.py`、`test_teams_approvals.py`、`test_teams_backends.py`、`test_teams_members.py`、`test_teams_worktree_resume.py`、`test_teams_tools.py`、`test_teams_policy.py`、`test_teams_integration.py`、`test_teams_lifecycle.py` |
| 集成回归 | `tests/test_agent_loop_teams.py`、`tests/test_cli_teams.py`、必要时 `tests/test_agent_loop_subagents.py`、`tests/test_cli_subagents.py`、`tests/test_worktrees_manager.py`、`tests/test_worktrees_git.py` |

## Phase 1：模型、路径和持久化（T1–T4）

### T1：Team/Member/Task/Message/Plan 模型与错误码

- **前置条件：** 已阅读现有 Agent/Permission/Session 类型；确认 session format 和 workspace fingerprint 规则。
- **允许文件：** `newcode/teams/__init__.py`、`newcode/teams/types.py`、`tests/test_teams_types.py`。
- **实现动作：** 定义稳定ID、roles、Team/Member/Task/Plan/Message/status/error enum、资源常量、事务commit/投影/交付证明模型；分离启动P0、只收窄Pt及业务审批At。错误码与spec逐operation契约一致；awaiting_approval不等于AgentMode.PLAN。
- **测试：** 同Task创建types tests；字段严格/边界/无secret runtime、全部合法与非法转换；paused/interrupted可校验恢复、integrated不可恢复，固定错误码。
- **完成判定：** 所有数据类型可 JSON-safe 表达且稳定序列化/反序列化；Phase 后续只依赖该模型。
- **禁止边界：** 不写文件、不创建 worker、不改现有通用模型/Provider。

### T2：Team 名称、路径与配置安全

- **前置条件：** T1。
- **允许文件：** `teams/paths.py`、`teams/config.py`、`tests/test_teams_paths.py`。
- **实现动作：** 固定 `~/.newcode/teams/<team-id>` 和 YAML 配置入口；校验 Team/member ID/name、workspace fingerprint、canonical containment、普通文件/目录、symlink/junction/reparse 与 ACL；strict user/project config merge，project 只能收窄；配置缺省禁用 Teams。
- **测试：** 同 Task 创建 `test_teams_paths.py`；覆盖绝对/相对 traversal、盘符/UNC、casefold collision、link escape、ACL unavailable、user/project merge、project 无法启用 Teams/coordinator/放宽限制。
- **完成判定：** 安全失败在创建/读取前发生，不产生 outside 目标副作用；config 所有字段和默认值有断言。
- **禁止边界：** 不读取 Provider secret、不修改 AppConfig/Permission 行为、不创建真实用户目录。

### T3：原子 TeamStore、跨进程锁与 CAS

- **前置条件：** T1–T2。
- **允许文件：** `teams/store.py`、必要时 `types.py/paths.py`、`tests/test_teams_store.py`。
- **实现动作：** owner OS锁内校验CAS，单个不可变事务含业务after-image/outbox/ID/幂等/sequence/全广播收件人清单；temp+fsync+排他发布commit及目录持久化后才更新投影并通知。实现启动replay先于读写、日志512KiB/事务及8,192条/64MiB上限；commit链损坏拒绝写，投影可重建。不以mtime回收锁。
- **测试：** 同Task创建store tests；事务生成/发布/投影各步崩溃，commit前不返回成功、commit后重放不重复业务操作；广播中途crash无丢收件人、sequence/幂等恢复；CAS/双进程lock/owner unknown/root替换/容量。有效journal与投影损坏分开测试。
- **完成判定：** lock/目录持久性不可证明fail closed；单文件替换不宣称多文件原子，重放以已提交事务为事实来源。
- **禁止边界：** 不删除 Team/Member 文件，不依赖内存锁代替跨进程锁。

### T4：脱敏成员 checkpoint 与恢复适配

- **前置条件：** T3；复用 Chapter9 `SessionArchive` 实际 JSONL 规则。
- **允许文件：** `teams/member.py`、必要时 `store.py/types.py`、`tests/test_teams_checkpoints.py`。
- **实现动作：** 每Member专用sessions子根；Chapter9兼容脱敏/工具配对/指纹JSONL与processed-message-ID元数据同次原子发布；绑定team/member/session generation，不能用独立receipt代替有效checkpoint；Lead邮箱同规则。恢复重建ChatSession与预算，不存临时授权。
- **测试：** 同Task创建checkpoint tests；secret/bad line/未配对截断/跨workspace/失败保留旧checkpoint；processed-ID与正文发布crash、被截断观察不可作为ack证明、Context摘要后去重元数据保留。
- **完成判定：** 每个 Member 独立 session archive；不写主 SessionArchive。
- **禁止边界：** 不启动 AgentLoop/后端、不改变 Chapter9 persistence 行为。

**Phase 1 gate：** `compileall newcode`；针对 `test_teams_types.py test_teams_paths.py test_teams_store.py test_teams_checkpoints.py`；`git diff --check`。

## Phase 2：任务、邮箱与计划审批（T5–T8）

### T5：共享 Task CRUD/state machine

- **前置条件：** Phase1通过。
- **允许文件：** `teams/tasks.py`、`store.py/types.py`、`tests/test_teams_tasks.py`。
- **实现动作：** task create/list/get/assign/claim/update/complete/delete；CAS revision；固定状态转换；只有 open、无人负责且无下游依赖才可删除；对相同 claim 幂等。
- **测试：** 同 Task 创建 task tests；并发 claim、重复操作、版本冲突、非法转换、权限角色边界、删除限制。
- **完成判定：** 错误操作安全错误码且无额外文件变更。
- **禁止边界：** 不实现自动调度，不由 task 完成直接启动/集成代码。

### T6：依赖 DAG 与可执行前置条件

- **前置条件：** T5。
- **允许文件：** `teams/tasks.py`、`tests/test_teams_tasks.py`。
- **实现动作：** 最多 16 个依赖，增边验证存在、无自依赖、无环；dependencies 未完成时 claim/start 拒绝。
- **测试：** task test 增加 missing/self/cycle/unfinished dependency/边界 case。
- **完成判定：** 每条依赖更新原子、并发无法提交循环。
- **禁止边界：** 不做复杂排序算法或跨 machine scheduler。

### T7：Mailbox durable message、broadcast、ack/replay

- **前置条件：** T3、T5。
- **允许文件：** `teams/mailbox.py`、`store.py/types.py`、`tests/test_teams_mailbox.py`。
- **实现动作：** runtime注入身份/时间；模型只能send普通kind，事务commit含完整广播/ID/sequence/幂等；read不ack。ack校验当前收件人、消息归属及同generation有效checkpoint中的ID，再提交ack事务；通知只是wake hint。
- **测试：** 同Task创建mailbox tests；sender/kind伪造拒绝、同键异正文冲突、广播中途崩溃replay、sequence/幂等一致；read不ack、他人/未checkpoint/旧generation ack拒绝、checkpoint后ack前崩溃只补ack；容量/脱敏/wake失败；明确投递去重不保证LLM或外部副作用exactly-once。
- **完成判定：** 已提交事务及可重建envelope/ack投影为事实；任一步crash不静默丢信、不重复业务变更。
- **禁止边界：** 不因通知失败丢消息或自动删除未 ack 数据。

### T8：结构化 Plan proposal 与 approval revision

- **前置条件：** T5、T7。
- **允许文件：** `teams/approvals.py`、`mailbox.py/types.py/store.py`、`tests/test_teams_approvals.py`。
- **实现动作：** 可信ApprovalService提交业务状态+系统outbox同一事务；actor来自runtime；修订使旧批准superseded并更新At，绝不修改P0/Pt/Permission。
- **测试：** 同Task创建approval tests；未知字段/伪造actor/系统kind、重复决策/旧revision/普通“同意”；各提交步骤crash恢复不重复批准；DO+awaiting_approval协调写入与PLAN不可写的区别。
- **完成判定：** 仅 `team_manage` 对当前 revision 的结构化 op 可改状态。
- **禁止边界：** Approval 不能写 Permission rule、cannot auto allow tools。

**Phase 2 gate：** `compileall newcode`；targets `test_teams_types.py test_teams_store.py test_teams_tasks.py test_teams_mailbox.py test_teams_approvals.py`；diff check。

## Phase 3：后端能力与启动/停止（T9–T11）

### T9：Backend protocol 与 capability/选择

- **前置条件：** Phase2通过。
- **允许文件：** `teams/backends.py`、`teams/types.py`、`tests/test_teams_backends.py`。
- **实现动作：** 固定protocol、precedence（explicit→team→user→lightweight）、限额/errors；probe仅静态/版本/模块--check/IPC能力检查，start才创建本次资源并验证，不混称无副作用。
- **测试：** 同Task创建backend tests；优先级、显式unavailable不fallback、probe零pane/socket/Worktree mutation、超时/错误、并发/队列上限。
- **完成判定：** probe失败零业务mutation；start失败走本次资源清理，failure code不含路径/环境。
- **禁止边界：** 不修改 Provider，不安装 tmux、不启动真实 agent。

### T10：Lightweight synchronous thread backend

- **前置条件：** T9。
- **允许文件：** `teams/backends.py`、`teams/runtime.py`、`tests/test_teams_backends.py`。
- **实现动作：** 以 bounded worker threads 为同步 AgentLoop 提供串行单-member execution/wake queue；每 CLI 最多 4 active; stop deadline shared; exception isolation。
- **测试：** fake callable 证明不启动重复 member thread、顺序、不阻塞调用者、cancel/timeout/shutdown 1s 上限、一个 member failure 不影响其它。
- **完成判定：** idle member 不消耗 model/tool request；worker 不直接写 Lead session。
- **禁止边界：** 不引入 asyncio/refactor AgentLoop，不绕过 AgentLoop event path。

### T11：Pane process、tmux argv 与 runtime-only IPC

- **前置条件：** T9–T10。
- **允许文件：** `teams/backends.py`、`teams/worker.py`、`teams/runtime.py`、`tests/test_teams_backends.py`。
- **实现动作：** 唯一入口`newcode/teams/worker.py`与`python -m newcode.teams.worker`；实际解释器用-I/-B、自检module origin及可信bootstrap cwd，--check无持久mutation。tmux多参数command直接exec Python/argv，不拼shell字符串/send-keys；不仅外层shell=False。IPC/pane验证peer/process/generation/唯一executor锁后才允许Worktree/执行；失败仅回收本次归属资源，secret仅IPC内存。
- **测试：** 实际Python模块--check/参数解析；假tmux检查argv边界/secret不入参、错误peer、重复executor；真实tmux可用时启动测试参数含空格/引号/分号/$/反引号均原样且sentinel零执行；在IPC/pane/握手/lease每步注入失败，验证清理且不影响既有资源。
- **完成判定：** 模块真实启动与pane真实执行分开取证；没有tmux/direct-exec/安全IPC证据则pane unavailable/未验证，不能凭mock标通过；lightweight仍可独立验证。
- **禁止边界：** 不自动安装、无 user shell string、无持久 secret、pane不是 OS sandbox。

**Phase 3 gate：** compileall；`test_teams_backends.py`；diff check。Pane 缺真实环境可继续 lightweight，但必须记录 pane unverified，不得将其能力标为可用通过。

## Phase 4：成员 AgentLoop、checkpoint/idle/recovery（T12–T15）

### T12：TeamMemberRunner 与独立执行上下文

- **前置条件：** Phase3通过。
- **允许文件：** `teams/member.py`、必要时 `runtime.py`、`tests/test_teams_members.py`。
- **实现动作：** 新建独立ChatSession/ContextManager/PermissionManager/Skill/usage/cancel，Definition必须worktree；保存启动B/P0及当前Pt/At。使用非交互confirmer，require_confirmation直接拒绝，不能调用主终端或继承Lead session allow。无目录隔离证明时member禁用MCP和Hook shell/HTTP；独立client/runtime不当隔离证明。用角色adapter端口，不传父session。
- **测试：** 同Task创建member tests；隔离状态、无agent/嵌套Team；confirmation拒绝executor零调用、主confirmer零调用、父session allow无效；MCP client/discovery零创建、Hook副作用/队列零执行、stdio cwd不被隐式改动；角色端口fake验证。
- **完成判定：** Member 正常 final 与 WorkerResult 关联但不写 Lead ChatSession。
- **禁止边界：** 不改 Provider；不共享 SubAgentManager state/Permission grants。

### T13：Natural final → checkpoint → idle 与 mailbox wake

- **前置条件：** T12。
- **允许文件：** `teams/member.py`、`runtime.py`、`mailbox.py`、`tests/test_teams_members.py`。
- **实现动作：** final checkpoint后同一业务事务更新idle+系统通知；自动投递先checkpoint再ack，模型read观察在下次请求前checkpoint但不自动ack。pause/stop关闭auto_wake，显式resume校验后才重开；stop真实退出后释放运行对象。跟踪in-flight工具/写入及executor lease，安全点屏障不以idle/cancel标签替代。
- **测试：** checkpoint/idle/outbox每步crash，checkpoint-before-ack、read后不ack、checkpoint后ack前崩溃去重；pause/resume、duplicate wake、未退出工具不能取得静止证明；系统kind无法由模型伪造。
- **完成判定：** 同一 runtime resume 不创建第二执行者。
- **禁止边界：** Worker 不写 Lead SessionArchive/ChatSession，不把消息只放内存队列。

### T14：跨 CLI restore、预算和权限重建

- **前置条件：** T4、T13。
- **允许文件：** `teams/member.py`、`store.py/types.py`、`tests/test_teams_checkpoints.py`、`tests/test_teams_members.py`。
- **实现动作：** 进程启动后 runtime lock 验证前任退出；restore SessionArchive checkpoint、tool exchange截断、Context预算；状态 `interrupted→starting→idle/running`; 重建 mode/permission/Skill/Context，不恢复 session allow；`integrated` 状态拒绝旧 Worktree resume。
- **测试：** corrupt/stale/wrong workspace/old runtime lease refuse; outstanding tool call truncate; resource cap; restore denies old Permission session allow.
- **完成判定：** failed restore 不覆盖 checkpoint、不启动 Provider。
- **禁止边界：** 不跨设备或跨用户恢复。

### T15：Team-specific Worktree lease restore

- **前置条件：** Chapter14 Worktree ownership API 已读；T14。
- **允许文件：** `newcode/worktrees/manager.py`、`git.py`、`types.py`、`teams/member.py`、`tests/test_teams_worktree_resume.py`、必要时 `tests/test_worktrees_manager.py`。
- **实现动作：** 增加 Team-owner-only resume API；交叉验证 Team manifest、owner marker、task/member/team id、canonical repo/common-dir/path、branch/head/unique registration、activity lock；恢复后取得新 runtime lock。
- **测试：** 同 Task 创建 resume tests；跨进程 valid claim、duplicate process lock、altered owner/head/branch/path/common-dir/registration reject with no remove/write。
- **完成判定：** 不确定时不复用、不退回 shared，保留 worktree。
- **禁止边界：** 不开放通用 Worktree recovery，不开放 child Git 写入或 run_command。

**Phase 4 gate：** compileall；`test_teams_checkpoints.py test_teams_members.py test_teams_worktree_resume.py test_subagents_worktrees.py test_worktrees_manager.py`；diff check。

## Phase 5：角色 tools、AgentLoop 与 CLI（T16–T19）

### T16：固定 Tool schema 与角色注册矩阵

- **前置条件：** Phase4通过。
- **允许文件：** `teams/tools.py`、`agent/mode.py`、必要时 `tools/registry.py`、`tests/test_teams_tools.py`。
- **实现动作：** 逐行实现spec五工具固定契约：每operation的角色、必填/可选/禁止字段、合法状态、读写、结果/错误；所有身份runtime注入，target/recipient仅为目标，拒绝模型team/actor/sender/decision_maker。按混合工具整体serial/non-read-only分类。
- **测试：** 同Task创建tools tests，以契约表为完整参数化矩阵逐operation覆盖成功/缺字段/跨operation字段/伪造身份/角色/状态/结果/错误；schema不按成员变化，普通会话/SubAgent零协作工具。
- **完成判定：** 普通会话与普通 SubAgent 无 Team collaboration tools。
- **禁止边界：** 不修改 ToolScheduler 算法、不允许工具自行扩权。

### T17：AgentLoop mode/Permission/ToolScheduler gate

- **前置条件：** T16。
- **允许文件：** `agent/loop.py`、`mode.py`、`teams/tools.py/runtime.py/member.py`、必要时 `permissions/normalizer.py`、`manager.py`、`builtins.py`；`tests/test_agent_loop_teams.py`、`tests/test_teams_policy.py`。
- **实现动作：** 主线程发布认证不可变Pt，P0不变/只收窄，At独立业务门；每请求及executor前用版本屏障重算交集。DO+awaiting_approval仅固定协调写，PLAN仅status；旧响应在计划修订/批准失效/mode或Skill收窄后重验，pane失联fail closed。成员confirmation非交互拒绝，普通工具仍经原链。
- **测试：** 同Task创建loop/policy tests；批准不能扩P0/恢复Pt/grant；旧请求tool在修订、Do→Plan/Skill收窄后零执行；同步/后台/pane fake confirmation零task/文件/Git/MCP执行及主confirmer零调用；Permission/调度顺序不变。
- **完成判定：** tool denied 时 TeamStore/Worktree/executor mutation零调用。
- **禁止边界：** 不绕过 Permission/Plan/Do/ToolScheduler，不改 Provider。

### T18：静态 `/team` 命令与 CLI attach lifecycle

- **前置条件：** T17。
- **允许文件：** `newcode/commands/builtins.py`、`newcode/cli.py`、`teams/runtime.py`、`tests/test_cli_teams.py`。
- **实现动作：** 注册 `/team lead|open|status|close` 固定 command；只更新当前 CLI role/Team attachment；Team create仍经 Lead tool；open workspace fingerprint验证；clear/resume detach并有界 pause/checkpoint。
- **测试：** 同 Task 创建 CLI tests；命令参数、unknown team、workspace mismatch、clear/resume不串 team scope、静态命令优先、不走 Provider。
- **完成判定：** 无 Team 功能的既有 CLI 保持原行为；普通 CLI tools unchanged。
- **禁止边界：** 不加自定义命令、slash command不直接执行 Git/AgentLoop。

### T19：Coordinator 双重授权和工具收窄

- **前置条件：** T16–T18。
- **允许文件：** `teams/config.py`、`coordinator.py`、`tools.py`、`cli.py`、`tests/test_teams_policy.py`、`tests/test_cli_teams.py`。
- **实现动作：** user config AND env双授权；封闭allowlist仅read_file/find_files/search_code+五固定Team工具及CLI本地命令。禁ordinary agent/load_skill/Skill runner、全部MCP、Hook shell/HTTP及任何未知入口；Team管理/集成是明确委派而非原始写入授权。
- **测试：** 双开关/项目不能开启；直接集合与启动委派P0分离，member仅在明确P0+Permission内写自身Worktree；父mode/Skill收窄仍生效。禁止入口schema/旧响应双拒绝、MCP discovery及排队Hook零执行、进入前在途动作未退出则不成功；批准/Skill/prompt不能恢复写权限。
- **完成判定：** coordinator has no arbitrary filesystem/shell write access。
- **禁止边界：** 不通过 tmux/backend/config绕过双重开关。

**Phase 5 gate：** compileall；`test_teams_tools.py test_teams_policy.py test_agent_loop_teams.py test_cli_teams.py test_agent_loop.py test_agent_loop_permissions.py test_agent_scheduler.py test_cli_commands.py`；diff check。

## Phase 6：Team-only Git integration 与受控 coordinator 操作（T20–T22）

### T20：Integration B/T/M、冻结快照与静止证明

- **前置条件：** Phase4 Worktree resume、Phase5 Team integrate tool/Permission。
- **允许文件：** `teams/integration.py`、`worktrees/git.py`、`newcode/cli.py`仅注入单写者用户确认端口、`tests/test_teams_integration.py`。
- **实现动作：** 校验启动B、实际目标symbolic分支T/HEAD M、revision/owner；取得所有executor lease/零in-flight屏障，idle/cancel标签不足。用户确认单写者前提；Manager锁不约束外部Git/编辑器。B→index→最终工作树生成安全manifest，支持新增/删除/普通二进制、记录stage中间hash；固定容量/secret/link/attributes拒绝。preview只读返回digest；integrate重复捕获匹配后原子冻结blobs，目标HEAD/index/内容摘要前后重验。
- **测试：** 同Task创建integration tests；非main分支/B缺失或改变、真实运行中工具/失联worker、锁竞争、目标dirty/ignored碰撞、secret/link/submodule/超限；staged+unstaged/删除/二进制/rename表示；快照扫描被外部修改拒绝；preview零持久mutation。
- **完成判定：** 相对B的冻结结果可验证，expected_target_head/digest不匹配拒绝；completed不等于测试或集成授权。
- **禁止边界：** 不调用 shell、不以message/task completion代替授权。

### T21：B→S、M→I三方集成及目标完整同步/崩溃恢复

- **前置条件：** T20。
- **允许文件：** `teams/integration.py`、`worktrees/git.py/manager.py/types.py`、`tests/test_teams_integration.py`。
- **实现动作：** 严格执行spec阶段表：B临时source Worktree仅用冻结blobs形成S；M临时integration Worktree三方merge S形成I；校验S/I parent/tree/事务标记后持久化applying_target，再在真实目标cwd执行固定ff-only，让branch/index/working tree一同更新并复核。业务journal保存integration ID/幂等/B/T/M/digest/S/I/阶段；每阶段crash按表只读识别已写commit，unknown不自动重试或回滚目标。只允许在本次临时merge状态abort，失败保留；成功成员integrated且旧base不可恢复写入。
- **测试：** 两member同B、第二次目标含第一个提交仍保留非冲突成果，重叠冲突保留；删除/二进制/staged+unstaged；非main目标及目标branch/index/worktree完整同步；每Git调用前后和阶段记录crash、S/I识别防重复commit、safe_not_applied重新确认、partial target/outcome_unknown；外部HEAD/index/工作文件变化、hooks零执行、Permission拒绝零mutation。
- **完成判定：** 幂等依靠日志+可验证Git对象而非盲重试；目标状态不完整即unknown并保留全部成果。不宣称跨Git/文件系统原子，不reset/clean/force/删分支。
- **禁止边界：** Child Git 仍 read-only/fail-closed；不访问 remote、无 arbitrary argv。

### T22：Coordinator integration operation only

- **前置条件：** T19、T21。
- **允许文件：** `teams/coordinator.py/tools.py/integration.py`、`tests/test_teams_policy.py`、`tests/test_teams_integration.py`。
- **实现动作：** Coordinator只可调用 fixed TeamIntegrationManager，要求 Lead explicit integrate op + Permission gate；没有 Manager/Permission capability时unavailable。
- **测试：** coordinator封闭集合，无write/command/ordinary agent/Skill runner/MCP/Hook shell/HTTP旁路；integration仅同一Manager；Permission拒绝零Git写，未知能力默认拒绝，plan/Skill仍收窄。
- **完成判定：** coordinator没有任意 shell或文件写入等价路线。
- **禁止边界：** 不把 Git 管理命令加入 Agent 通用 shell工具。

**Phase 6 gate：** compileall；`test_teams_integration.py test_teams_policy.py test_worktrees_git.py test_worktrees_manager.py test_worktrees_command_sandbox.py`；diff check。

## Phase 7：生命周期、全回归和最终验收（T23–T26）

### T23：Team/CLI shutdown 与 session switch

- **前置条件：** Phase1–6通过。
- **允许文件：** `teams/runtime.py/member.py`、`cli.py`、`tests/test_teams_lifecycle.py`、`tests/test_cli_teams.py`。
- **实现动作：** EOF/exit/KeyboardInterrupt/startup/runtime exception/clear/resume统一停止或checkpoint；总等待<=1秒后继续既有 SubAgent→Hook→Memory→Context→Worktree→MCP cleanup；失败隔离、旧 session通知丢弃。
- **测试：** 同 Task 创建 lifecycle tests；每路径 cleanup order、shutdown timeout/error isolation、member checkpoint/lease preserve、double close idempotent。
- **完成判定：** cleanup failure 不阻断后续已有服务，Team/PID不冒充 stopped。
- **禁止边界：** 不改变既有服务 cleanup owner/order semantics 除明确 required insertion。

### T24：跨章节 targeted regression 与静态审计

- **前置条件：** T23。
- **允许文件：** 默认无；仅直接失败证明的最小 Team 文件和对应 test。
- **实现动作：** Provider无Team import；审计固定operation契约、P0/Pt/At重验/confirmation、coordinator封闭集合、成员MCP/Hook隔离、业务journal/checkpoint/ack崩溃顺序、B/S/M/I集成与单写者局限、pane模块/direct-exec证据；运行Chapter4–15 targeted。
- **测试：** 逐条记录边界/skip；普通会话MCP保持兼容，Team成员缺隔离时禁用；Hook placeholder零Manager调用；不把mock/独立client当真实sandbox/pane证据。
- **完成判定：** 每条边界都有实现/测试证据，未知注明未验证。
- **禁止边界：** 不做无关重构，不用 code review代替 runtime evidence。

### T25：全量 compileall/pytest 与 `git diff --check`

- **前置条件：** T24 pass。
- **允许文件：** 默认无，缺陷修复限最小 Team+test。
- **实现动作：** 全量 `compileall newcode`、`pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter15-final"`、`git diff --check`。
- **测试：** 全量 pytest，逐个记录 skip 原因；项目现有 `.venv`。
- **完成判定：** 无失败；skip环境原因明确，不隐藏。
- **禁止边界：** 不暂存/提交/push/global git config。

### T26：受控 CLI/local Git acceptance 与 pane 证据

- **前置条件：** T24–T25 pass。
- **允许文件：** 默认无；仅 acceptance发现的最小直接缺陷。
- **实现动作：** fake Provider、fake/lightweight backend、临时 home/workspace/local Git repo验收 create team → spawn → tasks/message/approval → idle/wake/resume → preview/integration → close/reopen → clear/resume/EOF/error cleanup；验证 no external network/remote。
- **测试：** 实际模块--check可独立取证；tmux/安全IPC可用时真实pane direct-exec及特殊字符原样/零注入、错误peer/重复executor/失败清理、成员mailbox/idle/wake/shutdown实测；不可用则pane unavailable/未验证，仅报告lightweight单独验收。local repo覆盖两个member同B与删除/二进制/目标并发/阶段crash。平台symlink/reparse/锁限制逐项记skip。
- **完成判定：** 主文件不被未经授权修改；Integration只有经过Permission的固定 Manager事务；所有不确定资源保留。
- **禁止边界：** 不安装 tmux，不使用生产secret/第三方MCP/生产hooks，不push。

**Phase 7 gate：** compileall、Chapter4–15 targeted、全量pytest、静态审核、local Git/CLI acceptance、diff check均记录结果；任何关键隔离/集成不确定状态列为未完成。

## 执行顺序

```text
T1 -> T2 -> T3 -> T4
              -> T5 -> T6 -> T7 -> T8
                               -> T9 -> T10 -> T11
                                                -> T12 -> T13 -> T14 -> T15
                                                                         -> T16 -> T17 -> T18 -> T19
                                                                                                  -> T20 -> T21 -> T22
                                                                                                                   -> T23 -> T24 -> T25 -> T26
```
