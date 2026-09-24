# Chapter 14：Git Worktree —— 验收清单

> 每项须有测试输出/本地 repo 证据；未验证的 OS、Git 或路径安全能力标记为未通过，不以代码审阅替代执行证据。

## 通用安全前置

- [ ] 确认使用临时 home/workspace 和临时本地 Git repository；未访问 remote、未 push、未使用生产 secret/真实 MCP/生产 Git hook/tmux。
- [ ] 确认实现没有 `os.chdir`、shell 拼接、`shell=True` Git 调用、`git worktree prune`、force/remove/reset/clean/fetch/push。
- [ ] 确认当前 shared Definition/Fork Agent 行为与 Chapter 13 回归一致。
- [ ] 确认 Worktree 不被描述为 OS sandbox；若 command sandbox 未实测通过，则 Worktree child 的 `run_command` 零执行、不可见或明确 fail closed。

## Phase 1：模型、路径、Git runner、配置（T1–T4）

- [ ] T1：Definition 默认 isolation 为 `shared`；合法值 `worktree` 可读，非法值只隔离对应定义，旧 Definition/Fork 行为不变。
- [ ] T2：目录路径由 `<repo>/.newcode/worktrees/<agent-slug>/<task-id>` 生成；slug/task/branch独立校验；空段、dot段、过长、反斜杠、绝对/盘符/UNC、junction/symlink与root escape均拒绝。
- [ ] T3：Git adapter 仅固定 argv、`shell=False`、固定 cwd、有限 timeout/输出；每条 Manager Git argv 都强制 empty hooks path 并禁用 system/global config；Git stderr/异常脱敏，网络/任意命令零调用。
- [ ] T4：`~/.newcode/worktree.yaml` 建立权限上限，项目 `.newcode/worktree.yaml` 只能缩小；hard deny 优先；`.gitignore` 只新增 `.newcode/worktrees/`，不忽略指令文件/其它 `.newcode` 内容。

Phase 1 命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_subagents_discovery.py tests/test_worktrees_paths.py tests/test_worktrees_git.py tests/test_worktrees_config.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter14-phase1"
git -c safe.directory=F:/agent/Newcode diff --check
```

**Phase 1 退出門：** 4 项全通过；path/reparse containment 不确定则停止，不创建 Worktree。

## Phase 2：创建、ownership、注册和状态（T5–T8）

- [ ] T5：Worktree lease/status/owner marker/进程与跨进程锁只包含安全字段，不写 SessionArchive。
- [ ] T6：临时 repo 上 `worktree add -b` 创建后 manager 复核注册；repo/user/global fixture `post-checkout` hooks sentinel 均未执行；已存在路径或 branch 不覆盖、不启动 child。
- [ ] T7：恢复验证同时匹配 manager lease、owner marker、canonical top-level/common-dir、branch、HEAD 和唯一 worktree registration；只读 Git 查询也不触发 fixture hooks；单凭目录存在绝不复用。
- [ ] T8：创建中断只回滚本次创建且 ownership/registration/cleanliness全通过资源；冲突/状态未知时保留；清理失败不影响其它 task。

Phase 2 命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_worktrees_git.py tests/test_worktrees_manager.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter14-phase2"
git -c safe.directory=F:/agent/Newcode diff --check
```

**Phase 2 退出门：** local Git create/list/verify 与故障注入都通过；所有 Manager Git 命令的 fixture hooks 均零执行（包含 add/checkout）；无 force/prune/remote；否则停止。

## Phase 3：cwd、工具、Permission 与缓存根隔离（T9–T14）

- [ ] T9：ToolContext/child context 的 canonical `workspace_root`、`cwd`、identity明确分离；默认 cwd 兼容旧 root；无 `chdir`。
- [ ] T10：`read_file/write_file/replace_in_file/find_files/search_code` 使用 child root；相对 traversal、absolute path、symlink/junction escape拒绝；主 workspace同名文件不受写入。
- [ ] T11：Permission request、normalized path、sandbox root全是 child Worktree root；Plan/Do、hard deny、Permission gates不变。
- [ ] T12：read cache 的 key 含 canonical root、worktree/task identity、absolute file path 与 stat signature；不同 Worktree相同相对路径不命中。
- [ ] T13：`.git` pointer验证指向当前 task admin dir；只允许受限只读 `status --no-optional-locks`、`diff --no-ext-diff --no-textconv`、`log/show/cat-file/ls-files`、受限 `rev-parse`。`git add/commit/update-ref`、ref/config/worktree 写操作及未列命令拒绝；当前 task admin（含 index/HEAD）与共享 metadata 对 child 全只读。`run_command` 不执行原始 shell；越权写入零副作用，解析/ACL/hooks不能证明则子进程零启动。
- [ ] T14：现有 context artifact/Memory/instructions/Skill派生状态按 canonical source root区分；child artifact只在自己的 sandbox root写入并清理；Memory/Archive/Hook/parent Skill state不共享。

Phase 3 命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_worktrees_command_sandbox.py tests/test_subagents_worktrees.py tests/test_tools_workspace.py tests/test_permissions_sandbox.py tests/test_context_artifacts.py tests/test_memory_store.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter14-phase3"
git -c safe.directory=F:/agent/Newcode diff --check
```

**Phase 3 退出门：** 五个结构化 file/search tools全通过；Git只读 allowlist与 `add/commit/update-ref` 等写命令拒绝矩阵通过；common objects/refs及当前 index/HEAD、config/logs、main/other Worktree admin metadata对 child 均只读，越权操作零副作用；command sandbox/解析/hooks无法证明隔离即保持 fail closed，不可进入宣称完整 Worktree command support。

## Phase 4：配置初始化、Runner 和结果（T15–T18）

- [ ] T15：本地 tracked/ignored copy均是精确文件 allowlist；默认空、project只能缩小、秘密/runtime路径即使 allowlisted仍拒绝；无递归目录复制。
- [ ] T16：dependency link仅 user trusted canonical read-only targets；未知/outside/writable/无法验证的link不创建，安全继续。
- [ ] T17：Chapter 14 hooks execution allowlist固定为空；repo/user/global hooks sentinel在每种 Manager Git命令（特别 `worktree add`）下均零执行；empty hooks override缺失时 Git命令零启动。
- [ ] T18：Worktree Definition先创建并验证 lease再启动child；所有结束路径finally finish；返回脱敏状态/branch/path；Fork/shared原行为保持。

Phase 4 命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_worktrees_setup.py tests/test_subagents_worktrees.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter14-phase4"
git -c safe.directory=F:/agent/Newcode diff --check
```

**Phase 4 退出门：** setup任何失败均不泄露文件或破坏主 Agent；结果无diff、secret、stderr/stack trace。

## Phase 5：变更保护、过期清理、CLI lifecycle（T19–T22）

- [ ] T19：tracked/staged/untracked/ignored 文件或目录、领先 upstream commit导致 preserve；child 创建 ignored file后不得自动删除；no upstream、offline、Git错误/timeout、扫描不完整/状态未知都 preserve；只清理逐项验证的本任务 temp 后重新检查；不 fetch。
- [ ] T20：30天以上只是候选；path/Git ownership/activity三层门全部通过，且再次确认 tracked/staged/untracked/ignored 均空、root scan完整、upstream安全后才删；main/user/root外/link/unknown/active都拒绝。
- [ ] T21：EOF、`/exit`、KeyboardInterrupt、异常、startup异常、`/clear`、`/resume`都取消或有限等待旧 task；Worktree cleanup失败不阻断Hook→Memory→Context→MCP。
- [ ] T22：只有父 AgentLoop主线程安全点按序一次投递结果；session/generation匹配；collect通知互斥；Worktree state不写父archive。

Phase 5 命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_worktrees_manager.py tests/test_subagents_worktrees.py tests/test_cli_worktrees.py tests/test_agent_loop_subagents.py tests/test_cli_subagents.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter14-phase5"
git -c safe.directory=F:/agent/Newcode diff --check
```

**Phase 5 退出门：** dirty/ahead/ignored/untracked/unknown永不自动删除；ignored scan不完整视为 unknown；各退出 cleanup有限且失败隔离；否则停止最终验收。

## Phase 6：完整回归与 local acceptance（T23–T26）

- [ ] T23：Chapter 14、SubAgent、tools、Permission、Plan/Do、Context、Memory、MCP、Skill、Hook、Commands、CLI targeted regression通过，逐项记录 skip原因。
- [ ] T24：`compileall newcode` 与全量 pytest通过；所有skip有环境/fixture原因，不隐藏失败。
- [ ] T25：静态审计确认 Provider不知Worktree、无shell Git injection、每条 Manager Git命令禁hooks、child root贯通；Git命令矩阵只读 allowlist 生效，add/commit/update-ref及未列写操作被拒绝且零副作用；common objects/refs、当前 index/HEAD、main/其他 Worktree admin metadata均不可写；command sandbox有证据或 fail closed；MCP cwd兼容；cleanup三层门含 ignored scan；Cache根隔离；shared行为不变。
- [ ] T26：fake provider + 临时 repo演示 shared 与 Worktree Definition；读写不污染main；完成后 clean tree被清理，dirty tree保留并返回branch/path；cancel/timeout/clear/resume/EOF/exit/exception安全退出。

Phase 6 targeted 与全量命令：

```powershell
.venv\Scripts\python.exe -m compileall newcode
.venv\Scripts\python.exe -m pytest tests/test_worktrees_paths.py tests/test_worktrees_git.py tests/test_worktrees_config.py tests/test_worktrees_manager.py tests/test_worktrees_setup.py tests/test_worktrees_command_sandbox.py tests/test_subagents_worktrees.py tests/test_subagents_types.py tests/test_subagents_discovery.py tests/test_subagents_manager.py tests/test_subagents_policy.py tests/test_subagents_runner.py tests/test_subagents_tool.py tests/test_subagents_budget.py tests/test_agent_loop.py tests/test_agent_loop_permissions.py tests/test_agent_loop_subagents.py tests/test_agent_scheduler.py tests/test_agent_modes.py tests/test_cli_worktrees.py tests/test_cli_subagents.py tests/test_tools_file.py tests/test_tools_search.py tests/test_tools_command.py tests/test_tools_executor.py tests/test_permissions_sandbox.py tests/test_permissions_manager.py tests/test_permissions_modes.py tests/test_context_artifacts.py tests/test_context_manager.py tests/test_memory_store.py tests/test_memory_service.py tests/test_mcp_manager.py tests/test_mcp_permissions.py tests/test_skills_loader.py tests/test_skills_state.py tests/test_hooks_engine.py tests/test_commands_registry.py tests/test_commands_dispatcher.py tests/test_deepseek_provider.py tests/test_provider_tool_calls.py tests/test_cli.py tests/test_cli_session.py tests/test_cli_mcp.py tests/test_cli_hooks.py tests/test_cli_context.py tests/test_cli_memory.py tests/test_cli_skills.py tests/test_cli_commands.py -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter14-regression"
.venv\Scripts\python.exe -m pytest -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter14-final"
git -c safe.directory=F:/agent/Newcode diff --check
```

**最终退出门：** targeted + full pytest + static audit + local Git/CLI acceptance全部通过；hooks sentinel零执行、共享 Git metadata隔离与 ignored-file preserve 均有回归证据；不使用真实网络、生产 secrets、第三方 MCP、生产 hooks、tmux或全局 Git config。任何不确定状态列为未完成。

## 需记录的证据

- 每个临时 repository 的 `git worktree list --porcelain`、branch、HEAD、common-dir 检查结果（测试输出只保留脱敏必要字段）。
- 主 workspace和 child root的文件内容对照；sandbox拒绝结果。
- command sandbox实际证明或 fail-closed零启动证据。
- dirty/ahead/no-upstream/unknown的保留证明及三层过期过滤证据。
- CLI退出事件及cleanup顺序、每项是否有界、failure isolation。
- 全量 pytest通过数、每个skip原因、最终 diff check结果。
