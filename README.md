# NewCode

NewCode 是一个以 Python 编写的终端 AI 编程助手。它通过 AgentLoop 协调模型请求、工具执行和权限检查，并提供本地会话、上下文管理与受控 Agent 协作能力。

## 快速开始

要求 Python 3.10 或更新版本。若使用 `py -3.10` 创建虚拟环境，必须已安装 Python 3.10；也可把版本号替换为本机已安装且符合要求的版本。已有可用的 `.venv` 无需重建，直接使用其中的 Python。项目使用 `pyproject.toml` 声明依赖和 `newcode` 命令入口；默认从当前目录读取 `config.yaml`，也可用 `--config` 指定配置文件。

在 PowerShell 中，从项目目录运行：

```powershell
# 仅在尚无可用 .venv 时创建；py -3.10 要求已安装 Python 3.10
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:DEEPSEEK_API_KEY = "<your-api-key>"
.\.venv\Scripts\newcode.exe --config .\config.yaml
```

请在当前 PowerShell 会话中设置自己的 key，不要将真实 key 写入仓库或配置文件。当前仓库的 `config.yaml` 指向 `https://api.deepseek.com`，并配置模型名 `deepseek-v4-pro`；这仅反映本地配置，项目没有可复核的真实服务请求记录，不能据此保证该服务或模型当前可用。启动后可输入例如：

```text
请概述当前项目的目录结构，并指出 Python 程序入口。
```

## 主要功能

- **基础工具：** `read_file`、`write_file`、`replace_in_file`、`find_files`、`search_code` 和 `run_command`。工具经 AgentLoop 调度；文件访问受 workspace 路径检查约束，带副作用的调用还受 Plan/Do 与 Permission gate 约束。
- **AgentLoop 与 Plan/Do：** 统一处理模型响应、工具调用和结果回灌。Plan Mode 隐藏普通副作用工具；Do Mode 的可见性不代表自动获准，调用仍须经过权限检查。
- **Permission：** 支持配置化规则、权限模式和需要时的确认。子 Agent 的确认是非交互 fail-closed，不借用主会话的临时授权。
- **MCP：** 支持配置的 stdio 与 Streamable HTTP server、工具发现和调用。CLI 当前在启动阶段发现已配置的 server（coordinator 模式除外），并复用进程内连接；不是首次调用时才延迟加载。相关自动化测试使用 fake/local fixture，没有真实第三方 server 或外部网络验收。
- **上下文与记忆：** 支持工具结果外置、上下文压缩、会话归档/恢复，以及 user/project scope 的本地记忆注入。
- **命令：** 静态命令通过统一 registry/dispatcher 解析。未知 slash 命令不会转交模型。dispatcher 有 Tab 补全逻辑及单元测试，但当前 CLI 的输入路径尚未接入 Tab 键补全。
- **Skill：** 支持内置、用户和项目 Skill 的发现、按需加载、激活状态和受限工具集合。
- **Hook：** 支持生命周期事件、结构化条件、受控动作、失败隔离与有界关闭。HTTP Hook 需用户级显式网络授权；SubAgent Hook action 仍是不可用占位，不会创建子 Agent。
- **SubAgent：** 支持定义式和 Fork 式子任务、受限任务生命周期、后台结果通知及 Worktree 运行策略。
- **Worktree：** 可由内部 Manager 创建、验证和清理受控 worktree，并提供本地 Git 集成路径。Worktree child 的 `run_command` 和 child Git 写操作保持 fail closed；当前没有经验证的 OS 级进程沙箱。
- **Teams：** 支持持久团队、成员、共享任务与依赖、邮箱消息、计划审批、成员 checkpoint/恢复和受控本地 Git 集成。lightweight 后端使用有界线程执行；coordinator 需要用户级配置授权与 `NEWCODE_TEAM_COORDINATOR=1` 同时满足。

## 常用命令

以下为当前 CLI 注册或专门处理的命令。`/team` 仅在 Teams 启用时可用；`/exit`、`/quit` 和 `exit` 是 CLI 退出路径。

| 命令 | 用途 | 示例 |
|---|---|---|
| `/help` | 查看命令帮助 | `/help session` |
| `/compact` | 手动压缩当前上下文 | `/compact` |
| `/clear` | 安全保存后开始新会话 | `/clear` |
| `/plan`、`/do` | 切换交互模式 | `/plan` |
| `/session` | 列出或恢复会话；别名 `/sessions`、`/resume` | `/session list`、`/resume <session-id>` |
| `/memory` | 查看记忆元数据 | `/memory project` |
| `/permission` | 查看权限摘要 | `/permission` |
| `/status` | 查看会话和服务状态 | `/status` |
| `/review` | 将固定审查请求送入 AgentLoop | `/review` |
| `/team` | 进入 Lead、打开团队、查看状态或关闭 attachment | `/team lead`、`/team open <team-id>` |

Skill 激活后，符合条件的 Skill 还可提供受控短命令；这些不是任意用户自定义命令。

## Teams 最小使用流程

Teams 默认关闭。若要启用，在用户配置 `$HOME\.newcode\teams.yaml` 中加入：

```yaml
enabled: true
backend_default: lightweight
require_plan_approval: true
```

配置 user scope 后，在项目目录启动 NewCode，输入本地命令 `/team lead` 进入 Lead attachment。该命令只切换本地角色，不会创建团队；随后用普通文本描述团队任务，例如“创建一个团队，拆分任务并派生成员完成文档检查”。团队创建、任务分配和成员派生由模型通过固定 Team tools 请求，并继续经过现有模式、Permission 和执行边界。需要计划审批的成员须经结构化审批后才能执行受限操作。

lightweight 团队派生成员使用 Definition。仓库必须是可用的非 bare Git 仓库，已有至少一个 commit 且处于有效分支上；Git 必须能从进程 PATH 启动。Worktree 目录位于 `.newcode/worktrees/`（仓库 `.gitignore` 已忽略此目录）。创建失败时不会回退到 shared 或主工作区运行。

Definition 的项目发现目录是 `.newcode/agents/`，用户目录是 `$HOME\.newcode\agents\`。单个 Markdown 文件必须使用严格 frontmatter；`name`、`description`、`tools.allow`、`max_iterations`、`permission_mode` 是必填字段，`isolation` 可选，示例中显式设为 `worktree`。以下示例只声明当前基础 registry 中存在的工具：

```yaml
---
name: doc-editor
description: 在隔离工作树中检查并编辑项目文档。
tools:
  allow:
    - read_file
    - find_files
    - search_code
    - write_file
    - replace_in_file
max_iterations: 4
permission_mode: inherit
isolation: worktree
```

在 frontmatter 之后，Markdown 正文作为成员的操作说明，例如：

```text
按用户任务检查相关文档；只在必要时修改文件，并报告修改内容。
```

该成员可读取、搜索和编辑自己的 Worktree 文件，写入仍受父级工具范围及 Permission 约束。示例未授权 `run_command`；Worktree child 的命令工具当前 fail closed，不能用 shell 或 Git 写命令绕过此限制。

使用流程：在用户配置启用 Teams 后启动 NewCode；输入本地命令 `/team lead` 进入 Lead attachment（这不会创建团队）；再用普通文本要求模型创建团队、创建/分配任务并派生 `doc-editor` 成员。模型通过 Team tools 发起这些操作；需要审批的计划须由 Lead 结构化批准。`/team open <team-id>` 用于打开当前 workspace 已绑定的团队；`/team status` 查看 attachment 状态，`/team close` 关闭 attachment。

lightweight 团队功能已通过 fake Provider、临时 home/workspace 和本地 Git 仓库的自动化验收。

## 项目目录

- `newcode/agent/`：AgentLoop、事件与模式控制。
- `newcode/tools/`、`newcode/permissions/`：基础工具、workspace 检查和权限策略。
- `newcode/providers/`：模型 Provider 抽象与 DeepSeek/OpenAI-compatible 实现。
- `newcode/mcp/`：MCP 配置、runtime、server 管理与工具适配。
- `newcode/context/`、`newcode/memory/`、`newcode/persistence.py`：上下文管理、本地记忆和会话归档。
- `newcode/commands/`、`newcode/skills/`、`newcode/hooks/`：命令、Skill 和生命周期 Hook。
- `newcode/subagents/`、`newcode/worktrees/`、`newcode/teams/`：子 Agent、受控 worktree 和持久团队能力。
- `tests/`：fake Provider、fixture、临时目录与本地 Git 仓库测试。

## 验证与限制

最近一次 Windows 本地全量测试结果为 **1181 passed、13 skipped、0 failed**。跳过项主要是当前环境无法创建 symlink/reparse fixture，以及没有安装 tmux 导致的真实 pane 测试。lightweight Teams 流程有自动化验收；真实 tmux/pane、symlink 相关平台行为、非 Windows 平台和真实断电恢复没有全部验证。

自动化测试以 fake Provider、临时 home/workspace、本地 Git 仓库和本地 fixture 为主。代码包含真实 DeepSeek/OpenAI-compatible 请求路径，但没有可复核的真实外部模型或 MCP 服务验收记录。不要将代码路径或 fake 测试结果等同于外部服务可用性。

Worktree child 的 `run_command` 会在启动子进程前拒绝执行，child Git 写操作也不开放；内部受控集成不等同于任意 shell/Git 代理。当前 CLI 尚未接入 Tab 补全。项目没有可用于宣称延迟、成本节省、长时间运行成功率或其他性能比例的测量数据。
