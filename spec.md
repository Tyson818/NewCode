# Chapter 15：Agent Teams —— 持久化团队与协作

## 背景与源码事实

NewCode 当前的 `AgentLoop.run()` 是同步事件迭代器，Provider 通过同步 `stream_chat()` 返回事件迭代器；当前只有 MCP runtime 自己拥有 asyncio loop。ToolScheduler 将只读工具分批并发、将副作用工具串行执行。Chapter 13 SubAgentManager 是进程内、有界的一次性 worker/任务管理器，任务结果回到父 AgentLoop 安全点；它没有团队花名册、长期任务/邮箱、成员 idle 后保留对话并恢复的语义。

Chapter 9 的 SessionArchive 提供脱敏 JSONL、workspace fingerprint、坏行跳过与不完整 tool exchange 截断。Chapter 14 WorktreeManager 只恢复本进程 lease；child `run_command` 目前因缺乏 OS 级进程 sandbox 而 fail closed，child 不允许 Git 写操作。CLI 的 Hook → Memory → Context → MCP cleanup 已有顺序与异常隔离。Chapter 12 Hook 的 `subagent` action 当前明确是 `hook_subagent_not_available`，本章不改变它。当前依赖没有 tmux；环境中也未检测到 tmux 命令，因此文档不声称独立 pane 后端已实测。

本章新增的持久团队、团队成员 runtime、协作工具和受控集成是新增能力，不是上述现有能力的别名。Provider interface 保持对 Team 无感知。

## 目标与角色

团队是绑定单一 canonical workspace 的持久实体，有稳定 Team ID、Lead、成员 roster、共享任务、计划审批记录与邮箱。成员可以自然完成一轮后进入 `idle`，保留脱敏 session checkpoint；新消息可令其从当前进程中的原上下文继续，或在新 CLI 进程中从 checkpoint 重建上下文。每个成员有独立 ChatSession、ContextManager/artifact root、PermissionManager、Skill state、取消状态、usage 统计和 Worktree lease；不得共享父 session 或权限授权。

角色工具集合固定如下，按角色注册，不按成员或任务动态改 schema：

| 角色 | Team 工具 | 普通工具 |
|---|---|---|
| 普通 CLI 会话 | 无 | 既有工具集合 |
| Team Lead | `team_status`、`team_manage`、`team_tasks`、`team_message`、`team_integrate` | 受当前 Plan/Do、Permission、Skill whitelist 和成员/模式政策收窄后的普通工具 |
| Team Member | `team_status`、`team_tasks`、`team_message` | 由其有效 Definition allowlist 与父策略、Plan/Do、Permission 等共同决定 |
| 普通 SubAgent | 无 | Chapter 13 子 Agent 原有工具交集；不得嵌套 Team |

`team_status` 为只读；其余 Team tool 的 schema 含持久化/状态写操作，整体按非只读注册并串行调度（具体 operation 是否写入见契约表）。Team 状态变化的锁/版本比较是最终并发保护。Team 工具无论角色均经过 AgentLoop mode/visibility、PermissionManager、ToolScheduler、executor。`AgentMode.PLAN` 只暴露 `team_status`，不暴露 Team 写工具。`awaiting_approval` 是成员业务状态，不是 PLAN 的别名：成员可以在 DO + awaiting_approval 下使用已授权的提交计划/消息协调能力，但普通文件、命令与外部副作用仍受审批门禁止；处于 PLAN 时必须由用户显式切换 DO 才能使用这些协调写操作，批准本身不改变 mode。

普通 CLI 只有经静态 `/team lead` 或 `/team open <team-id>` 明确进入 Lead 角色后才取得 Lead tools；`/team close` 脱离团队并安全暂停/收尾当前成员。打开团队时必须核对用户级 Team store 与当前 workspace 身份。新建团队仍由 Lead 的固定 `team_manage.create` 操作完成；不创建用户自定义命令。

## 身份、路径与持久化

- Team store 固定在 `~/.newcode/teams/<team-id>/`。Team ID 由安全随机数生成，为 `team-` + 32 位小写十六进制；不得以用户输入拼成路径。团队 display name 限 80 个 Unicode 字符、去控制字符且非空；独立 slug 为 `[a-z][a-z0-9-]{0,47}`。Member ID 为 `[a-z][a-z0-9_-]{0,31}`；重名按 casefold 拒绝。task/message/session 文件名均由生成 ID 产生。
- Team 与唯一 workspace fingerprint 绑定。fingerprint 为 canonical workspace root 与 canonical Git common-dir 的规范化 SHA-256；manifest 不需保存可注入路径。跨 workspace `/team open` 与 worker resume 返回 `team_workspace_mismatch`，不迁移、不复制团队数据。
- Team 根、每级子目录与文件都必须验证普通目录/普通文件、canonical containment、无 symlink/junction/reparse point 逃逸；无法识别 reparse point、ACL 或 owner 时 fail closed。路径检查在创建、读取、原子替换、恢复及清理前重做。
- POSIX 根目录/文件权限为用户专有；Windows 必须验证只有当前用户和系统管理主体可访问。平台无法证明私有 ACL 时禁止写入 Team store。
- 推荐布局：`team.json`（versioned manifest/roster/revision）、`transactions/<commit-sequence>-<transaction-id>.json`（业务提交事实）、`tasks/<task-id>.json`、`messages/<message-id>.json`、`acks/<member-id>.json`（可重建投影）、`members/<member-id>/sessions/<session-id>.jsonl`、`integrations/<integration-id>/` 与 `locks/`。只保存 JSON-safe、已脱敏的业务状态和 Chapter 9 兼容会话记录；不保存 API key、MCP headers/tokens、完整 env、Permission session allow、Provider client、Tool/Manager/Context 对象、PID 或其他长期进程句柄、IPC credential 或 Hook/Skill runtime object。仅短暂的 OS 锁 owner 记录可含 process identity、启动 nonce 与 scope；owner 释放并验证后删除，不能当作恢复凭据。
- 文件采用同目录临时文件、flush/fsync、原子 replace；新 ID 文件用排他创建。更新由 TeamStore revision/CAS 与跨进程 advisory lock 串行化。锁记录 owner process identity、启动 nonce 和 scope；只能由内核锁证明所有者已释放后清除，绝不单凭 mtime 判断 stale。锁超时/owner 无法确认时安全拒绝且不覆盖。
- 跨进程锁须在当前 OS 上有自动化竞争测试；不支持安全 advisory locking 时，独立进程后端不可用，不能改用仅靠目录存在或 mtime 的“锁”。

## 团队配置与资源上限

现有 `config.yaml` 是 Provider/workspace/timeout/Permission 的 AppConfig；本章不把 Team secret 或动态规则扩展到该文件。新增严格 YAML：用户级 `~/.newcode/teams.yaml` 建立能力上限；可选项目级 `<workspace>/.newcode/teams.yaml` 只能收窄用户配置，不能启用 Teams/coordinator、增加资源上限、放宽审批或权限。

- Team 功能默认关闭；用户级配置显式 `enabled: true` 后可使用 `/team`。新团队默认 backend 为 `lightweight`，成员默认需计划审批、Definition 必须显式声明 `isolation: worktree`。
- user config 可设置 `backend_default: lightweight|pane`、低于固定上限的 `max_members` / `max_running_members` / `require_plan_approval`；project config 只能更保守。未知字段或类型错误以稳定脱敏诊断隔离，不从 project 自动打开功能。
- `coordinator.enabled` 仅接受用户级 true，且用户启动 CLI 时显式设置 `NEWCODE_TEAM_COORDINATOR=1` 才生效；两者缺一即关闭。project 文件不能开启。环境变量值不是 Team state，不落盘。
- 固定限制：每个用户最多 32 个 Team；每 Team 最多 8 个 worker members（另 1 个 Lead）；每 CLI session 最多 4 个正在运行/恢复的 members；member 启动等待队列最多 16；Team task 最多 256 条；每 task 最多 16 个 dependency；每 Team 最多保留 4,096 封消息、未确认邮箱最多 1,024 封；正文最多 16 KiB UTF-8、task 标题 240 字符、说明 8 KiB；member checkpoint 最多 8 MiB。每Team提交日志最多8,192条或64 MiB，任一触顶拒绝新事务，不自动压缩/删除提交证据。触顶时稳定拒绝新写入，不静默丢弃未确认内容。
- 沿用 Chapter 13 单次 member activation 的 8 轮、300 秒、16K 近似 token 上限；每次恢复重新计算，累计 usage 仅作统计，不恢复超额授权。一般 wait 上限 30 秒。Team shutdown 使用单一全局 1 秒等待预算，不按成员数累加。

## 后端与成员生命周期

`AgentLoop`/Provider 同步 API 不为“协程”概念重构。`lightweight` backend 在本进程有界 worker 中以每 member 单线程串行驱动 AgentLoop；idle member 等待 mailbox signal，不在 idle 时发模型请求。当前 CLI session 关闭时取消、checkpoint 并释放该进程 worker；Team 数据留存，下一次 open 重新构造 runtime。

`pane` backend 的唯一文件入口为 `newcode/teams/worker.py`，模块路径为 `python -m newcode.teams.worker`。成员输出不作为消息事实来源。无副作用 probe 只检查 tmux 可执行文件/版本、可信绝对 Python、worker 的 `--check` 模块自检、OS 锁/IPC 能力，不创建 pane/socket、不运行 Agent。随后 startup validation 才创建本次 IPC/pane、握手并取得唯一 executor lease；通过后才初始化 Worktree 并启动成员。

tmux 必须支持并经真实测试证明多参数 command 的直接 exec：外层固定 argv 为 `tmux split-window -d -P -F '#{pane_id}' -c <trusted-bootstrap-cwd> -- <absolute-python> -I -B -m newcode.teams.worker --team-id <generated-id> --member-id <validated-id> --endpoint <generated-endpoint> --generation <runtime-generation>`，所有command/args分别传递，不能合成shell-command字符串。bootstrap cwd/模块来源必须来自可信NewCode安装而非成员Worktree；`-I`隔离Python路径、`-B`禁止启动写bytecode，同一解释器/参数执行--check并核对module origin；未安装/来源不可信则unavailable，不能导入成员文件冒充worker。--check不创建用户配置或运行对象。成员工具cwd仍显式传Worktree，不使用进程chdir。要求tmux内层exec argv，禁止sh -c、cmd /c、send-keys或插值；仅外层shell=False不算证据。任何平台/版本不能证明此行为则pane unavailable。非secret参数也逐项校验；特殊字符原样传递测试不得执行额外命令。

启动验证必须绑定 OS peer identity、已启动 worker process、member/generation 与唯一运行锁；错误 peer/重复 executor 拒绝。任一步失败只终止本次已验证归属的 worker/pane、关闭 IPC 并撤回本次 provisional 状态；不碰既有 pane/lease，清理不确定则保留占用标记和诊断。worker 不可用、pane 创建失败或 secure IPC 不可用时返回稳定错误；不得静默改用 lightweight。默认 lightweight，不自动安装 tmux/其它软件。tmux pane 不是 OS sandbox。当前无 tmux 时 pane unavailable/未验证，lightweight 可独立验收，不得宣称两种后端均已验收。

pane worker 不得在命令行、tmux environment、持久配置或 Team 文件传递 API key/MCP headers。CLI 建立当前用户 ACL 限定的本地 named pipe（Windows）或权限 0600 的 Unix socket，使用 OS peer identity 校验同一用户和已登记 member；运行期 Provider/MCP 配置只经此通道进入 worker 内存。若平台未验证 IPC ACL/peer 身份或配置安全传递，pane backend fail closed。仅限制同一 OS 用户之外的威胁；Team 不声称抵御同用户恶意进程。

能力选择顺序固定：调用参数明确指定 backend > 已持久化 Team backend > 用户级 `backend_default` > `lightweight`。只探测选中的后端；显式选择不可用即失败，不 fallback。无副作用 probe 必须先于业务持久 mutation；有资源创建的 startup validation 不称为无副作用，失败按本次资源清单回滚。Worktree 创建和成员执行在 startup validation 成功之后。

Team 状态为 `ready → active → closing → ready|closed`，`closed` 只表示显式归档/关闭，不删除资料，之后可重新打开。Member 状态：`created → starting → awaiting_approval|running → pausing → paused → resuming → running`，以及自然完成 `running → idle`；完成受控集成后转终态 `integrated`。非终态可进入 `stopping → idle|interrupted|failed`。自然最终回答后 checkpoint 成功才 `idle`；pause 在安全点 checkpoint 后才 `paused`。Provider/tool 异常为 `failed`；checkpoint 失败为 `failed` 且保存上一个有效 checkpoint；进程崩溃后运行锁确认释放，未完成 runtime 状态变为 `interrupted`，不伪装 idle。`failed/interrupted` 不能直接恢复执行，必须先校验 checkpoint、工具交换、策略与 Worktree ownership，再经显式 resume 转 `starting`。`integrated` member 不可在旧 base 上恢复写入；需继续工作必须创建新 member/worktree。非法状态迁移返回稳定码。

同一时刻每member只有一个执行者。自然idle可消息唤醒；pause/stop把持久`auto_wake`设false，显式resume并校验成功后才重新打开，不能用新邮件绕过暂停。stop在真实退出后释放运行对象，不删除checkpoint。CLI重启先验证checkpoint/tool-pair、Worktree owner/registry和当前策略，再创建runtime。恢复得到新PermissionManager、ToolContext、ContextManager、Skill state、取消flag和usage accumulator；不恢复Session allow/确认、Hook pending injection、Provider client或运行句柄。Context prepare在恢复/新请求前重算预算；不完整工具交换依Chapter9截断。

## 共享任务、消息邮箱与审批协议

### Shared Tasks

Task 字段：生成的 `task_id`、title/description、creator、optional assignee、status、dependency IDs、revision、created/updated timestamps、可选 completion summary。状态为 `open → assigned → in_progress → blocked|completed|cancelled`；`blocked → assigned|in_progress|cancelled`。任何终态不可被重复完成或删除。删除仅允许 open、无 assignee、无被引用下游依赖的 task。任务更新要求 `expected_revision`，CAS 冲突返回 `team_task_conflict`；同一成员重复 claim 是幂等返回，其他成员已领取则冲突。依赖必须存在、不能自依赖，增边前检测循环；任何依赖未 completed 时拒绝 claim/执行。Team 本章不自动调度 task 到 Agent，不实现跨机队列或复杂 DAG scheduler；Lead 显式分配并触发成员。

### Mailbox

`TeamMessage` 固定字段：`message_id`、`team_id`、由 runtime 注入的 `sender_member_id`、recipient member ID 或 `broadcast_id`、kind、脱敏正文/结构化 payload、UTC 时间戳、sequence、read/ack 状态和安全摘要。模型不能指定/覆盖 sender、team、时间、sequence 或 read 状态。recipient 只能从当前 roster 选择；广播在锁内按 roster 顺序生成 idempotent per-recipient envelope。

模型 `send/broadcast` 只能产生 `team.message.v1`，不能提供 kind。`team.task.assigned.v1`、`team.task.completed.v1` 只由 TaskService，`team.plan.submitted.v1`、`team.plan.decision.v1` 只由 ApprovalService，`team.member.idle.v1`、`team.member.failed.v1` 只由 MemberRunner 产生。接收同名协议只是读取已提交业务状态的通知，不再次执行业务操作；伪造 kind/身份安全拒绝。自然语言“同意/批准”不修改 approval。

单文件 atomic replace 不提供多记录事务。本章采用最小提交日志方案：所有 task/plan/member 状态变化、关联 outbox、消息分配、idempotency 映射及 ack 更新，都在 Team owner lock 下生成单个有界事务记录（上限 512 KiB），记录 expected/new revisions、业务 after-image、runtime mutation ID、幂等键、消息 ID、完整广播收件人 envelope 清单和 sequence 区间。事务 ID/消息 ID 使用安全随机生成；消息 sequence 在锁内按已提交最大值递增，广播按当时 roster 的固定顺序分配。模型可提供的 idempotency_key 只是不超过 64 个 ASCII 安全字符的请求标签，实际键为 runtime team + sender + operation + key；同键异正文/收件人返回 `team_idempotency_conflict`。

提交顺序固定：①校验锁/容量/revision并生成完整事务；②同目录临时写入、flush/fsync，再排他发布不可变transaction commit文件并确保目录项持久化；此时才是唯一业务提交点；③更新可重建tasks/messages/acks/manifest/幂等索引投影；④返回成功/发送wake hint。`commit_sequence`为每次事务递增的独立序号并带previous-commit-ID链；消息`sequence`为单独递增的整数，事务保存分配区间和新高水位，无邮件事务不消耗消息序号。目录持久性不能证明则store unavailable。提交前崩溃忽略临时文件且未返回成功；提交后投影/wake前崩溃，启动先在锁内按commit序列校验重放，再服务读写，不重新分配ID/sequence、不重复业务变更。投影缺失可重建，提交记录损坏/revision链不完整则fail closed，不跳过后继续写。日志/投影容量不足拒绝新事务，不删未确认邮件，不做在线日志压缩。邮箱事实来源为已提交事务与其envelope投影，wake失败不影响提交。

模型 `read` 不自动 ack。自动投递先把 envelope 加入成员 session，在同一份有效 Team checkpoint 中保存消息及 processed-message-ID 元数据，再持久化 ack 事务；模型 read 的 ToolResult 则先按原序进入 session，runner 在下次主请求前持久化包含该观察的 checkpoint，随后模型才可显式 ack。有效 checkpoint 使用原子发布的完整 JSONL，沿用 Chapter9 配对/脱敏规则；processed ID 元数据与消息正文在同一发布单元，Context 压缩不能删除该去重元数据。Lead 自身邮箱同样使用绑定当前 Lead session 的 Team checkpoint adapter；不是只写一个与 session 脱节的 receipt 文件。

ack 必须检查runtime当前收件人、消息确为其envelope、checkpoint绑定同Team/member/session generation，且ID已在该有效checkpoint；未入checkpoint、旧generation、他人消息均拒绝。重启先校验旧checkpoint并重建当前generation的有效checkpoint，再补ack，不能直接使用旧generation receipt。checkpoint前崩溃重新投递；checkpoint后ack前崩溃只补ack、不重复追加；ack已提交则幂等返回。重复inbox scan按sequence和message ID去重。恢复坏行/配对截断会使相应delivery proof无效，不能据它ack；不覆盖原始有效checkpoint。未确认消息不淘汰。这里保证的是持久消息投递去重，不保证LLM请求或外部工具副作用exactly-once；调用后checkpoint前崩溃须报告可能重复，不自动重放副作用。

### Plan Approval

Plan proposal 字段为生成 `plan_id`、member ID、monotonic `revision`、内容、submitted timestamp、status `pending|approved|rejected|superseded`、decision member/Lead ID、decision timestamp 与关联 message ID。Member 每次修改 plan 必须创建 revision+1，新 revision 自动使旧批准 superseded。Lead 只可对当前 pending revision 通过固定 `team_manage.approve_plan|reject_plan` 操作提交 `plan_id + expected_revision + structured decision + bounded reason`；并发重复决策以 CAS 决定，旧 revision 返回 `team_plan_stale`。自由文本和邮箱文本不会生成批准状态。

需审批的 member 在 DO + awaiting_approval 下，普通能力只允许 `read_file/find_files/search_code`，协调能力只允许 `team_status`、`team_tasks.submit_plan` 及 `team_message.send/broadcast/read/ack`；每条 operation 仍需 Permission。协调写入限于受控 Team store，不是普通文件/外部副作用授权。固定 schema 可以含审批后 operation，executor 必须按 operation 拒绝审批前的 task 修改，不动态改 schema。PLAN 只保留只读工具与 `team_status`，不能靠业务审批状态放宽。

权限计算分三层：启动时不可变父级工具/Permission 上限 `P0`（包括明确委派的角色能力）；由 Lead 主线程发布、带 generation/revision 的当前收窄快照 `Pt`（单调取交集，不能撤销已收窄项）；当前计划审批门 `At`（可从待批变为已批，只解除业务限制）。实际能力每次主请求及 executor 开始每个工具前均重新计算：`Registry ∩ P0 ∩ Pt ∩ Definition/role allow − deny ∩ member mode ∩ Skill whitelist ∩ At ∩ isolation/backend/coordinator limits`，最后再经过独立 PermissionManager/hard deny/sandbox。普通工具 Definition allow 与固定角色委派集合分开建模，角色委派也受 P0/Pt 和所有 gate 限制。批准不能增大 P0、恢复 Pt 已移除工具或产生 Permission grant。主进程按 scope 向 pane 发布经过认证的快照，worker 不读取 Lead 可变对象；缺快照、失联或 generation 不符即拒绝新工具。

计划修订/批准撤销和父级收窄都递增策略版本；旧模型响应中的调用仍在executor开始前重验。策略发布与工具准入共享Manager屏障，pane每次工具必须经认证IPC申请当前版本的执行lease，不得只靠本地推送快照离线执行；失联拒绝。lease登记是逻辑准入点并计入in-flight，收窄后不得给旧许可新增准入；已准入调用尽力取消、等真实退出，不能声称撤销能回滚副作用，不能将它们误判为静止。所有后台/独立进程成员使用非交互confirmer：`require_confirmation` 必须返回安全拒绝、executor零调用，不调用主终端HITL、不自动允许、不继承Lead session allow。用户若要允许能力，须预先配置明确Permission规则。

成员运行对象不与Lead共享，独立实例本身也不证明文件隔离。MCP stdio cwd沿用现有配置语义，不能悄悄改成Worktree；本章缺少可验证的remote/MCP文件范围限制，因此Worktree成员禁用MCP工具（不创建client/session、不启动stdio/HTTP discovery）及Hook shell/HTTP action（含排队/旁路触发），直至另有经过验证的隔离能力获批，不以只读标记推断安全。独立Hook runtime只允许原有request-only prompt injection/诊断及unavailable subagent placeholder；独立Skill/Context/artifact不共享Lead MemoryService，成员Skill只收窄工具且不能嵌套ordinary agent或激活可逃逸runner。

## 工具 schema 与并发

五个 schema 固定且 `additionalProperties=false`。下表每行均必填 `operation`，另列该行必填/可选参数；**禁止字段=除 operation 与该行列出的必填/可选之外的全部字段**，不能把其他 operation 的合法字段带入。全局禁止 `team_id/member_id/sender/actor/role/decision_maker/session_id/generation/permission/mode/kind/sequence/read_state`；这些身份与授权字段只从可信 runtime 注入。`target_member_id`、`recipient` 是校验 roster 后的操作目标，不可用来改变调用者身份。审批决策者来自当前 Lead runtime，operation 本身表达 approve/reject。

公共类型：所有ID为经注册表验证的受控字符串；revision为正整数，未知ID不创建资源。`dependencies`是至多16项的不重复task ID字符串数组；`require_plan_approval`为bool。`limit`为1–50、默认20，cursor是当前Team+调用者+查询绑定的opaque token，过期/跨scope拒绝；每页正文合计最多16KiB，溢出带next_cursor。title/body/content/reason必填时非空，description允许空字符串；reason/summary最多2,000字符、content最多8KiB，其它沿用资源上限。`expected_target_head`是当前仓库object format的完整40/64位小写hex commit ID，`snapshot_digest`为64位小写hex。`backend=lightweight|pane`；`confirmation`必须bool true，仅表达模型操作意图，不替代用户确认；`idempotency_key`固定`[A-Za-z0-9_-]{1,64}`。

合法调用状态缩写：`L0`=用户已进入 Lead 角色但未绑定 Team；`L`=Lead 已绑定 ready/active Team；`LR`=Lead 已绑定 ready/active/closed Team；`M`=当前 executor lease/generation 有效的 running 或 awaiting_approval 成员；`MA`=M 且当前计划已批准或用户配置明确免审批。角色不在该行即拒绝；paused/idle/failed/interrupted/integrated 成员不执行模型工具，须先按生命周期唤醒。所有写 operation 仅 DO；M 的 awaiting_approval 仅允许下表明确标识的协调操作。`team_status` 可 PLAN/DO；混合读写工具整体串行/非只读，不因某个 read/preview 临时改变注册分类。

所有结果为安全ToolResult：成功data带当前revision，失败仅error_code与有限阶段标签。公共错误按顺序为参数 `team_arguments_invalid` → 角色/身份 `team_role_denied/team_scope_invalid` → mode/策略/审批 `disallowed_tool/team_policy_changed/team_approval_required` → `permission_denied` → 状态/CAS/存储 `team_state_invalid/team_revision_conflict/team_store_busy/team_store_corrupt`。校验失败不执行operation；需读状态才能决定的分支只允许受控只读查询。表中“写入”指持久业务/Git变更，不把读取期间OS锁的受控临时元数据算业务写入。下表增量错误加上公共错误适用于每行。

### team_status（只读）

| operation | 允许角色/合法状态 | 必填字段 | 可选字段 | 写入 | 成功 data | 增量错误 |
|---|---|---|---|---|---|---|
| status | Lead L0/LR；Member M | 无 | 无 | 否 | 当前绑定/角色、Team/member安全状态、容量；L0无绑定 | team_workspace_mismatch |
| roster | Lead LR；Member M | 无 | cursor, limit | 否 | 成员ID、角色、审批/运行状态及revision | team_cursor_invalid |
| tasks | Lead LR；Member M | 无 | task_id, cursor, limit | 否 | task_id给单项否则分页；Member仅所属Team安全任务摘要；单项时禁止cursor/limit | team_task_not_found, team_cursor_invalid |
| inbox | Lead LR；Member M | 无 | cursor, limit | 否 | 仅当前调用者邮件元数据/安全摘要；不投递正文、不ack | team_cursor_invalid |
| plan | Lead LR；Member M | plan_id | 无 | 否 | Lead可查所属Team；Member仅自己的计划正文/版本/状态 | team_plan_not_found, team_plan_access_denied |

### team_manage（Lead-only，串行）

| operation | 合法状态 | 必填字段 | 可选字段 | 写入 | 成功 data | 增量错误 |
|---|---|---|---|---|---|---|
| create | L0 | team_name | backend | 是 | 生成team_id、Lead绑定、revision；Lead身份runtime生成 | team_limit_exceeded, team_backend_unavailable |
| spawn | L；目标task为open、无assignee且依赖完成、无运行执行者 | member_name, agent_name, task_id, expected_team_revision | backend, require_plan_approval | 是 | 生成member_id、starting状态、revision；任务绑定新成员 | team_name_conflict, team_definition_invalid, team_task_not_ready, team_backend_start_failed |
| pause | L；目标running/awaiting_approval/pausing/paused | target_member_id, expected_member_revision | 无 | 是 | pausing或已paused；只在真实安全点checkpoint后标paused | team_member_not_found, team_member_busy |
| resume | L；目标idle/paused/failed/interrupted | target_member_id, expected_member_revision | task_id | 是 | resuming/starting与revision；task_id如给出须已分配给目标且依赖完成 | team_member_busy, team_task_not_ready, team_backend_unavailable, team_restore_invalid |
| stop | L；目标created/starting/awaiting_approval/running/pausing/paused/resuming/idle/interrupted/failed/stopping | target_member_id, expected_member_revision | 无 | 是 | stopping或已确认idle/interrupted/failed；不是杀进程已成功的虚假承诺 | team_member_not_found, team_shutdown_timeout |
| approve_plan | L；当前计划pending、所属成员非integrated | plan_id, expected_plan_revision | reason | 是 | 当前revision approved、业务审批门更新、decision消息ID | team_plan_not_found, team_plan_stale |
| reject_plan | L；当前计划pending、所属成员非integrated | plan_id, expected_plan_revision, reason | 无 | 是 | 当前revision rejected、decision消息ID | team_plan_not_found, team_plan_stale |
| archive | L；不存在未静止executor、无未决integration | expected_team_revision | 无 | 是 | closed、保留所有资料；活动时拒绝，先显式stop/pause | team_member_busy, team_integration_pending |

`member_name` 遵循 Member ID 名称格式但只是创建时的花名；runtime 分配稳定 member ID 并保存名称注册映射。`require_plan_approval` 只能设 true 加严，false 若弱于用户/项目政策则拒绝，不能由模型免审批。stop重复调用只对同一 runtime mutation ID 幂等，真实状态不明仍busy/timeout。

### team_tasks（串行）

| operation | 允许角色/合法状态 | 必填字段 | 可选字段 | 写入 | 成功 data | 增量错误 |
|---|---|---|---|---|---|---|
| create | Lead L | title, description, dependencies | 无 | 是 | open task_id、revision；dependencies可为空 | team_limit_exceeded, team_task_dependency_invalid, team_task_dependency_cycle |
| claim | Member MA；task open/assigned给自己，依赖完成 | task_id, expected_task_revision | 无 | 是 | assignee=runtime成员、assigned及revision；重复同人claim幂等 | team_task_conflict, team_task_not_ready |
| assign | Lead L；task open/assigned/blocked、依赖完成且无活动executor | task_id, target_member_id, expected_task_revision | 无 | 是 | 目标成员绑定、assigned和revision | team_member_not_found, team_member_busy, team_task_not_ready |
| update | Lead L；Member MA且task属于自己；task非终态 | task_id, expected_task_revision, changes | 无 | 是 | 更新task/revision；changes为下述严格对象 | team_task_conflict, team_task_dependency_invalid, team_task_dependency_cycle, team_task_not_ready |
| complete | Lead L；Member MA且task属于自己；task in_progress、依赖完成 | task_id, expected_task_revision, summary | 无 | 是 | completed/revision、系统completed消息ID；verification=not_verified | team_task_conflict, team_task_not_ready |
| delete | Lead L；task open、无负责人、无下游引用 | task_id, expected_task_revision | 无 | 是 | tombstone/revision，不删除审计提交 | team_task_delete_denied |
| submit_plan | Member M（允许DO+awaiting_approval）；task已分配给自己且非终态 | task_id, content | plan_id, expected_plan_revision | 是 | 初次生成pending plan_id/revision；修订使旧批准失效 | team_task_conflict, team_plan_stale |

`changes` 至少一项且 `additionalProperties=false`：Lead允许 `title/description/dependencies/status`，Member仅允许 `status`；status值仅 `in_progress/blocked/cancelled`，成员不可cancelled（须Lead处理）。in_progress必须当前assigned/blocked且依赖完成；blocked必须当前in_progress/assigned；cancelled须无活动executor。title/description/dependencies仅Lead且task open/assigned、无活动executor时可改。禁止更改assignee/ID/creator/completed。submit_plan初次禁止plan_id/expected_plan_revision；修订必须两者同时提供且计划属于runtime成员与task。任何不存在依赖、自依赖、环均拒绝。相同runtime mutation ID先返回原提交结果，不重新执行；claim同一成员且仍为assigned时可幂等返回当前状态，不能用此例外越过其它状态/revision保护。

### team_message（串行；read也不自动ack）

| operation | 允许角色/合法状态 | 必填字段 | 可选字段 | 写入 | 成功 data | 增量错误 |
|---|---|---|---|---|---|---|
| send | Lead L；Member M（允许awaiting_approval） | recipient, body, idempotency_key | 无 | 是 | 提交后的message_id/sequence；kind固定team.message.v1 | team_recipient_invalid, team_mailbox_full, team_idempotency_conflict |
| broadcast | Lead L；Member M（允许awaiting_approval） | body, idempotency_key | 无 | 是 | 固定roster收件人清单（除本人）、broadcast_id/envelope IDs | team_mailbox_full, team_idempotency_conflict |
| read | Lead L；Member M（允许awaiting_approval） | 无 | cursor, limit | 否 | 仅本人邮件正文/envelope/next_cursor；不改变ack状态 | team_cursor_invalid |
| ack | Lead L；Member M（允许awaiting_approval） | message_id | 无 | 是 | 本人有效checkpoint已含ID才acked；重复幂等 | team_message_not_found, team_message_not_owned, team_ack_checkpoint_required |

### team_integrate（Lead-only，串行；仅DO）

| operation | 合法状态 | 必填字段 | 可选字段 | 写入 | 成功 data | 增量错误 |
|---|---|---|---|---|---|---|
| preview | L；目标idle且所有executor/工具真实静止、lease有效 | target_member_id, expected_member_revision | 无 | 否 | B、实际目标branch/M、安全文件统计、snapshot_digest、verification=not_verified | team_member_busy, team_worktree_owner_invalid, team_target_dirty, team_snapshot_invalid |
| integrate | L；满足preview条件且单写者/目标HEAD/快照仍匹配 | target_member_id, expected_member_revision, expected_target_head, snapshot_digest, idempotency_key, confirmation | 无 | 是 | integration_id、阶段、B/M/S/I、target同步结果、保留成果、安全错误或not_verified | team_target_changed, team_single_writer_required, team_integration_conflict, team_integration_pending, team_integration_outcome_unknown |

target branch始终由当前绑定workspace实际symbolic HEAD解析，禁止模型指定分支、路径、argv或提交说明。preview不持久写snapshot；integrate重新捕获并比较digest，变化需重新preview。所有业务mutation带runtime生成的mutation ID；同ID只返回原提交结果，CAS和幂等映射不能由模型身份字段替代。五个工具每个operation均须有成功、缺字段、未知/跨operation字段、角色、状态、返回与错误矩阵测试。

工具内部锁负责同一 Team 的提交日志/revision修改；ToolScheduler 对混合写工具整体串行执行，来自pane的并发更新由store owner lock和事务提交序列化。所有写操作在Permission允许前零mutation；read/preview不生成持久化副作用。事务提交后投影失败按Mailbox恢复协议处理，不重新执行业务操作。

## Worktree 与受控代码集成

Team Member 默认且必须使用声明 `isolation: worktree` 的 Definition；`shared` Definition 不可用于可写 Team member，避免多个成员直接争用主工作区。member worktree 仍使用 Chapter14 root、owner marker、registration、锁和 dirty/ignored/ahead protect；child `run_command` 仍 fail closed，成员不能 `git add/commit/update-ref`。不能为了 Teams 放宽 Chapter14 child 权限。

现有 Chapter14 不支持跨进程 lease 恢复或 Git 写集成。Chapter15 必须新增仅用于 Team 的 `resume_team_lease` 校验 API，核对 team/member owner marker、当前 user Team manifest、canonical workspace/common-dir、path、branch、HEAD、唯一 registration 与跨进程锁；这不是通用 Worktree recovery。修改后无法确认唯一 ownership 时不复用，保留文件并返回错误。

### 固定基准、快照与单写者前提

Member启动时持久保存经验证的基准commit `B`（Worktree创建时HEAD）。集成目标是当前绑定workspace实际 `symbolic-ref HEAD` 分支 `T`，集成前记录HEAD `M`；不硬编码main。detached HEAD、B不可读取、member HEAD不再等于B、B不是M的ancestor、目标registry/branch不明则拒绝。preview digest绑定B/T/M与来源快照；切换目标分支即使HEAD相同也要重新preview。集成变更始终相对B，不把当前目标文件当成member旧版本直接覆盖。

集成前必须暂停所有member及Lead其它潜在写入者，取得唯一executor lease及worker barrier回执，验证对应generation的模型/工具/Hook/MCP调用实际退出、in-flight计数为0、无写句柄；当前负责集成的Manager控制调用不计入被暂停的成员调用。未返回的调用/失联pane/未知executor都拒绝。取消flag、idle标签或进程PID存在与否不足以证明静止。冻结期间禁止唤醒/新工具；同用户外部编辑器不受此屏障保护。

Manager锁仅协调NewCode自身。用户通过现有CLI confirmer交互必须接受“目标workspace及成员目录在本次事务期间由NewCode单写，外部Git/编辑器停止写入”的前提，确认绑定integration ID/B/T/M/digest；可与Permission确认同次展示。Permission规则allow不等于这项确认，模型confirmation字段也不能替代；没有交互/有效用户确认返回 `team_single_writer_required`。此确认不产生Permission grant，后台成员不调用它。程序前后重复校验branch/HEAD、index tree/校验和、tracked内容摘要和untracked/ignored冲突集合；外部变化被检测即停止，未知已写结果进入outcome_unknown。没有跨Git/ref/index/文件系统整体原子性承诺，也不能声称这些复核阻止了所有外部竞争。

冻结快照为 `B + path manifest + blobs`：收集B→index及index→working tree的tracked/staged/unstaged变化、非ignored新增和删除；以最终working-tree字节为集成结果，stage中间版本不单独提交但其hash计入来源验证。rename固定表示delete+add。支持普通文本及二进制regular file（最多1 MiB/文件、16 MiB/快照），删除记录存在性与B blob ID；超限、symlink/reparse、submodule、special、secret/runtime/`.git`路径、未知Git属性过滤器/外部diff/merge驱动全部拒绝。ignored文件不集成、不删除；任何与目标路径冲突的ignored内容拒绝。若只有index意图而最终树与B相同，则返回no_code_changes并保留原index，不把成员标integrated。双次扫描stat+hash与index验证稳定后，快照原子发布到受控integration根，记录digest、文件mode、B、member revision/generation及完整manifest；不能验证稳定就拒绝。之后只读冻结blobs，不再从可变source文件生成commit。

### 集成阶段与目标同步

`TeamIntegrationManager` 是唯一受控Git写能力，所有命令固定argv、explicit cwd、`shell=False`、有限超时、受控配置与环境；每次启动禁hooks、签名、外部driver/filters、remote访问，不使用模型命令/分支/提交说明。流程固定：

1. **authorized/frozen：** Lead显式调用，Permission允许、单写者确认、B/T/M/owner/registration/静止证明/revision及preview digest全部匹配；目标tracked/staged/untracked必须clean，ignored保留且无碰撞。权限拒绝在任何Git mutation前返回。生成integration ID，绑定runtime team/member + scoped idempotency key + snapshot digest + B/T/M。将阶段记录持久化后才写Git。
2. **source_committed：** 在Manager独占、禁hooks的临时source Worktree以B为基准应用冻结manifest（包含明确删除与二进制），固定literal pathspec `git add -A -- <validated paths>`并commit为 `S`，parent必须B、tree必须精确等于快照期望tree。受控commit标记含integration ID，提交元数据固定，不运行用户配置代码。绝不把快照文件直接复制到目标workspace。
3. **merged：** 另一个临时integration Worktree从M创建，用固定 `git merge --no-ff --no-commit S` 做基于共同基准的三方集成；冲突仅留在临时目录并返回 `team_integration_conflict`，不覆盖目标。合并成功后受控commit得到 `I`，验证parent为M/S及预期tree。两成员同B时，第二次仍创建B→S2，再将S2与已含第一成员提交的最新M合并，保留不冲突成果；相同路径冲突必须交回Lead，不自动选边。
4. **applying_target：** 重验真实静止和单写者条件，T仍指向M、目标index/worktree仍clean且hash一致；写入包含T/M/I/目标状态摘要的applying_target记录并持久化。随后**在目标workspace cwd**执行固定 `git merge --ff-only --no-edit I`，让Git同步更新目标分支、index和工作区；不使用单独 `update-ref` 冒充集成成功，不用force/reset/clean。Git失败/超时后只做只读核对，不能盲目重复或在目标执行abort/回滚。只有本次临时目录的merge状态可经验证后固定 `merge --abort`，abort失败也保留。
5. **completed：** 验证实际T/HEAD=I、index tree=I tree、tracked工作区与I一致、无新untracked冲突、ignored未受损、registration仍匹配，再持久化completed及系统结果。source member转终态integrated，保留其B分支/Worktree/checkpoint且不可继续写旧base；继续工作必须新建member/worktree。返回 `verification=not_verified`：task completed/成功merge均不代表已运行测试。

### 崩溃恢复与幂等

每个integration阶段都保存到Team提交日志及可重建record；S/I还须有integration ID、expected parent/tree/digest的可验证证据，Git共享对象/临时refs可能在失败时保留，不删除分支。固定恢复表：

| 最后持久阶段 | 恢复行为 |
|---|---|
| authorized/frozen，尚无目标写 | 校验冻结blob/lease，读取受控临时branch及commit标记识别已生成S；能证明归属和tree才续做，否则保留并失败；不因重试重复commit |
| source_committed/merged | 按S/I的parent/tree/事务标记重用；重新取锁和Permission/单写者确认，重验M。M变化则team_target_changed，需重新preview及新事务，不沿用旧批准；冲突保持临时资源 |
| applying_target | 在任何新Git写前只读核对：若T=I且index/worktree与I完全一致、归属与单写者事实可确认，则补completed；若T=M且原index/worktree摘要完全不变，标safe_not_applied，必须重新确认后显式续做；任何混合、外部变化、查询错误均outcome_unknown，禁止自动重试/回滚 |
| completed | 相同幂等键只返回原结果，不再次写Git；同键异快照/参数冲突 |
| conflict/outcome_unknown | 保留所有source/temporary/目标成果和阶段诊断，拒绝新的相关集成；需用户在本章外人工核对，不执行reset/clean/force/delete branch |

每个阶段及Git调用前后注入崩溃/超时测试；stage record与Git更新不能假定是同一事务。检测到外部并发变化时不追写目标、不自动撤销用户改变。集成期间冻结member的恢复/停止必须解除事务占用后按状态操作，unknown时保留占用并禁止继续写。用户未确认或状态未知始终保留成果。

## Coordinator 模式

Coordinator 是 Team Lead 的权限收窄子模式。仅用户级 `coordinator.enabled=true` 与用户主动设置 `NEWCODE_TEAM_COORDINATOR=1` 同时满足时打开；project 不能设置开关。能力采用封闭allowlist：普通读取 `read_file/find_files/search_code` 加五个固定Team工具（仍按其operation/角色/mode/审批契约），以及CLI原有纯本地生命周期/状态命令。Team管理与受控集成是明确授权的委派能力，不是普通文件写入许可。其它工具默认不可见且executor拒绝：包括write_file/replace_in_file/run_command、ordinary agent、load_skill/Skill runner、全部MCP（含未知/自称只读）、Hook shell/HTTP和任何未列出的副作用入口。request-only prompt注入不能改变该allowlist。没有可验证受控能力时拒绝，不能恢复原始shell；所有允许能力仍与Plan/Do、Permission、Skill whitelist和父级限制取交集。

Coordinator的直接能力集合与委派上限分开：member的P0取用户已授权Team委派范围、Definition及父级原有mode/Skill/Permission上限交集，启动即固定；不能把“Lead直接隐藏写工具”转换为任意委派权限，也不因该界面过滤将显式授权的Worktree文件委派抹除。member仍可在P0明确包含且独立Permission允许时写自己的Worktree；父mode/Skill/政策收窄持续发布Pt，计划批准不增加委派范围。禁shell/MCP/Hook副作用和嵌套Agent的隔离限制始终保留。coordinator不启动MCP discovery；角色进入前关闭旧副作用准入、重验排队Hook/旧工具调用并等待在途动作安全退出，无法证明静止则不宣告进入成功。

## CLI 与生命周期

- `/team lead` 进入无绑定 Team 的 Lead 模式；`team_manage.create` 成功后绑定当前 Team。`/team open <id>` 附着已存在且 workspace 匹配的 Team；`/team status` 仅显示安全状态；`/team close` 解除 Lead attachment 并停止/收尾本 CLI scope。命令注册为 Chapter10 静态命令，静态命令优先，不支持 overlay/custom command。
- `/clear` 与 `/resume` 在旧主 session checkpoint 前将成员取消到安全点、写 member checkpoint、将可恢复 member 设为 idle/interrupted，关闭 Team runtime/通知 generation 并 detach Team；不删除 Team/task/mailbox/worktree，也不把旧通知注入新 session。用户需显式 `/team open` 重新 attach。
- EOF、`/exit`、`/quit`、KeyboardInterrupt、正常退出、CLI 启动后异常均有限停止 Team workers/panes。team finalizer 使用总计 1 秒等待预算，failure-isolated，不阻塞现有 SubAgent → Hook → Memory → Context → Worktree → MCP 清理。无法停止的 pane 记录安全 `team_shutdown_timeout`、保持 lease/lock 标记为占用或 interrupted，不允许第二 worker 接管。
- 关闭/重启只停止 runtime，不删持久 roster/task/message/session。活动成员先收到取消并尽力 checkpoint；如果 checkpoint 未完成则保留上个完整文件和 Worktree，状态 interrupted。成员在下一次 CLI session 经 workspace/owner/Permission 重校验后才可恢复。
- 不对接 Chapter12 `subagent` Hook action；其 placeholder 继续返回 unavailable，零 Team 创建或调度。Team 生命周期不让 Hook action 递归启动 Agent。

## 错误模型、非目标与验收

安全错误码以工具契约表为完整operation错误集合，另含配置/生命周期错误 `team_config_invalid`、`team_path_invalid`、`team_store_unavailable`、`team_workspace_mismatch`、`team_limit_exceeded`、`team_worker_failed`、`team_shutdown_timeout`、`team_message_invalid`、`team_coordinator_unavailable`、`team_integration_failed`。Permission链复用 `permission_denied`，目标脏状态统一 `team_target_dirty`，不用main命名。UI/model/log 不暴露 secret、绝对用户路径、完整消息/工具输出、raw Git stderr、stack trace、IPC credential；诊断仅使用码与有限阶段标签。

非目标：远程 Team/跨设备同步、团队市场/插件、跨机器 scheduler、Agent Teams 自我编排、Agent Teams 之间嵌套、Worktree 间任意 Git 操作、成员 shell/Git commit 能力、自动 merge/冲突解决、自动验证代码、团队记忆合并、MCP/Hook 权限旁路、Provider 改造、自动安装 tmux。

验收标准：

- AC1：Team ID/路径/workspace 绑定与私有 store 防止跨 workspace、reparse/symlink escape、损坏文件覆盖；多进程锁竞争安全失败。
- AC2：user/project 配置合并和默认关闭符合权限单调收窄；coordinator仅双重授权且采用封闭能力集合，MCP/Hook shell/HTTP/ordinary agent/Skill runner未知入口不能旁路。
- AC3：唯一入口newcode.teams.worker可实际模块自检；无副作用probe与有资源startup分离；真实tmux内层exec argv/特殊字符零注入、错误peer、重复执行者、启动失败清理均有证据。无tmux时pane unavailable/未验证，lightweight单独验收，不宣称两backend均通过。
- AC4：任务状态、CAS、DAG、依赖门、删除限制并发测试通过。
- AC5：可信系统协议不可由模型伪造；read不ack、他人/未有效checkpoint消息不可ack；事务/幂等/sequence/完整广播/投影/有效checkpoint/ack各步崩溃恢复不静默丢信、不重复业务变更；投递去重不声称LLM/外部工具exactly-once。
- AC6：P0启动上限、Pt单调收窄及At业务审批分离；DO+awaiting_approval仅允许明确协调写入，PLAN不放宽。计划修改/失效、Do→Plan/Skill收窄后旧响应在executor前重验；批准不扩权；后台require_confirmation非交互拒绝且零执行，不继承Lead授权。
- AC7：member final → checkpoint → idle → 消息唤醒/重启恢复；pairing、预算、scope 和权限重建正确。
- AC8：五个固定工具每个operation的角色、字段/禁止字段、状态、读写、返回及错误矩阵全测；身份只runtime注入。原有执行链完整；Worktree成员MCP/Hook副作用无隔离证据时禁用，独立client不算隔离；普通SubAgent/会话无团队协作工具。
- AC9：固定B→S与M→I三方集成，实际目标分支、冻结快照、静止证明及Permission/单写者确认完整；更新目标branch/index/worktree并复核。两成员同B、目标已有另一成员提交、删除/二进制/staged+unstaged、目标外部并发、每阶段崩溃/幂等/unknown本地测试通过；无reset/clean/force/delete branch，保留成果且不声称整体原子或已验证代码。
- AC10：`/clear`、`/resume`、正常退出、异常、EOF、`/exit`、KeyboardInterrupt 清理有界且失败隔离。
- AC11：Provider 不导入 Team；Hook subagent placeholder 无动作；MCP/Skill/Permission/Plan/Do/Context/Memory/Command 语义回归。
- AC12：fake/local fixtures 的 Chapter 4–15 targeted、全量 pytest、compileall、diff check 和受控 CLI acceptance 全过；所有 skip 有原因。

测试必须使用 fake Provider/backend、临时 home/workspace、临时本地 Git repo 和 fake/本地 IPC。不得访问远端、不 push、不使用生产 secret、第三方 MCP 或生产 hooks、不修改全局 Git 配置。真实 pane backend 未执行时 checklist 必须写“未验证”，不能由 fake tmux mock 代替。
