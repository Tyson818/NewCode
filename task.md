# Chapter 10：Command Registry & Dispatcher 原子任务

## T1：命令模型与稳定错误

前置：无。允许：新增 `newcode/commands/__init__.py`、`types.py`、`tests/test_commands_registry.py`。动作：定义类别、元数据、可见性、解析/outcome 与安全码。测试：不可变定义、名称规范化、无敏感错误。完成：模型不依赖 CLI/Provider/工具。边界：不注册命令、不改 CLI。

## T2：静态注册中心

前置：T1。允许：`registry.py`、registry 测试。动作：静态注册、规范名/别名冲突启动失败、可见帮助、排序候选。测试：重复名、别名交叉冲突、casefold、隐藏过滤。完成：冲突零覆盖。边界：不得读取配置或运行时注册。

## T3：解析器与补全

前置：T1–T2。允许：`dispatcher.py`、dispatcher 测试。动作：区分普通文本、slash、未知/错误参数，实现唯一补全/多候选菜单/隐藏排除。测试：大小写、空白、未知、无副作用、参数尾部。完成：未知 slash 不成为 AI 输入。边界：不得执行 handler、LLM、工具或文件。

## T4：UI control interface

前置：T1。允许：`ui.py`、UI 测试。动作：定义渲染无关协议、CLI adapter、fake UI。测试：info/error/help/menu/mode 调用。完成：handler 不直接依赖 print。边界：不引入 TUI/GUI 依赖。

## T5：纯本地内置命令

前置：T2–T4。允许：`builtins.py`、内置测试，必要时最小安全状态 accessor。动作：实现 help/compact/clear/session（及旧别名）/memory/permission/status。测试：成功、错误、脱敏、session/Context/Memory 隔离。完成：无主模型/工具/MCP 调用。边界：不改 Context/Memory 策略。

## T6：模式与预设 AI 命令

前置：T5。允许：`builtins.py`、内置测试。动作：实现 plan/do state outcome 与无参数 review 固定 AI input outcome。测试：模式不改 Permission；review 非动态。完成：review 不直接调用 Provider。边界：不修改 PromptBuilder/Provider。

## T7：CLI dispatcher 接入

前置：T3–T6。允许：`newcode/cli.py`、`tests/test_cli_commands.py`、必要既有 CLI 测试。动作：exit 优先、分派 outcome、只有 ai_input 入 AgentLoop、迁移旧分支/别名。测试：命令循环、未知引导、普通文本、EOF、exit、checkpoint/finally。完成：既有注入仍可用。边界：不改 MCP/Provider/Permission 语义。

## T8：安全 gate 回归

前置：T7。允许：回归测试；源码仅失败直接证明时最小修复。动作：证明 review 经 AgentLoop，Plan/Do、Permission、ToolScheduler、MCP、Context、Memory 未绕过。完成：fake provider/fixture 通过。边界：无命令级权限或网络。

## T9：全量验收

前置：T1–T8。允许：仅失败直接证明的最小模块与回归测试。动作：compileall、全量 pytest、diff check、静态调用审计、受控 CLI 验收。完成：记录 skip 原因；不用网络、secret、第三方 MCP、tmux。边界：不进入 Chapter 11。

每项完成后：

```powershell
python -m compileall newcode
python -m pytest <该项 tests> -q -rs --basetemp "$env:TEMP\newcode-pytest-chapter10-tN"
```

失败、冲突不安全、绕过 gate 或需要范围外设计时，立即停止并报告。
