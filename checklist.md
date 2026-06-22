# NewCode Tool System Checklist

> 每一项都必须通过运行命令或观察行为验证。

## Implementation Completeness

- [ ] 普通对话仍然可用。验证：运行 `python -m pytest tests/test_cli.py`，期望 v0.1 CLI 对话相关测试通过。
- [ ] 模型请求工具时，NewCode 能识别工具名称和完整 JSON 参数。验证：运行 `python -m pytest tests/test_provider_tool_calls.py`，期望流式工具调用碎片能被拼接并解析。
- [ ] 可用工具会被转换成 OpenAI-compatible tools schema。验证：运行 `python -m pytest tests/test_tools_registry.py`。
- [ ] `read_file` 能读取工作区内文件。验证：运行 `python -m pytest tests/test_tools_file.py -k "read"`。
- [ ] `write_file` 能创建或覆盖工作区内文件。验证：运行 `python -m pytest tests/test_tools_file.py -k "write"`。
- [ ] `replace_in_file` 只在原文恰好匹配一次时修改文件。验证：运行 `python -m pytest tests/test_tools_file.py -k "replace"`。
- [ ] `find_files` 能按模式查找文件。验证：运行 `python -m pytest tests/test_tools_search.py -k "find"`。
- [ ] `search_code` 能返回匹配文件、行号和匹配行。验证：运行 `python -m pytest tests/test_tools_search.py -k "search"`。
- [ ] `run_command` 能执行非交互式命令并返回 exit code、stdout、stderr。验证：运行 `python -m pytest tests/test_tools_command.py`。

## Integration

- [ ] 工具注册中心默认注册六个核心工具。验证：运行 `python -m pytest tests/test_tools_registry.py`。
- [ ] 工具执行器能处理未知工具、参数错误和工具异常。验证：运行 `python -m pytest tests/test_tools_executor.py`。
- [ ] 工具结果会进入会话上下文。验证：运行 `python -m pytest tests/test_session.py`。
- [ ] CLI 能完成“一次工具调用 -> 结果回灌 -> 最终回复”流程。验证：运行 `python -m pytest tests/test_cli_tool_flow.py`。
- [ ] 每轮用户请求最多执行一次工具调用。验证：运行 `python -m pytest tests/test_cli_tool_flow.py -k "one_tool_call or no_loop"`。
- [ ] 多个工具调用会返回本阶段不支持的结构化失败结果。验证：运行 `python -m pytest tests/test_cli_tool_flow.py -k "multiple"`。
- [ ] 工具失败后 NewCode 不崩溃，模型仍能收到失败结果并生成回复。验证：运行 `python -m pytest tests/test_cli_tool_flow.py -k "tool_error or recover"`。

## Safety

- [ ] 文件工具不能访问工作区外路径。验证：运行 `python -m pytest tests/test_tools_file.py tests/test_tools_executor.py -k "outside or path_not_allowed"`。
- [ ] 默认不允许访问 `.git` 内部路径。验证：运行 `python -m pytest tests/test_tools_executor.py -k "git"`。
- [ ] 命令执行有超时保护。验证：运行 `python -m pytest tests/test_tools_command.py -k "timeout"`。
- [ ] 工具结果和错误信息不会泄露 API Key 明文。验证：运行 `python -m pytest tests/test_tools_executor.py tests/test_cli_tool_flow.py -k "sensitive"`。
- [ ] `replace_in_file` 匹配失败时不会留下半完成修改。验证：运行 `python -m pytest tests/test_tools_file.py -k "not_unique or zero_match"`。

## Build And Test

- [ ] Python 包能通过语法编译检查。验证：运行 `python -m compileall newcode`。
- [ ] 单元测试全部通过。验证：运行 `python -m pytest`。
- [ ] 自动化测试不依赖真实 DeepSeek API Key。验证：在未设置 `DEEPSEEK_API_KEY` 的环境中运行 `python -m pytest`。
- [ ] 如果 pytest 不可用，记录原因并运行 unittest fallback。验证：运行 `python -m unittest discover`。
- [ ] 若项目未配置 lint，则不要求 lint。验证：检查 `pyproject.toml`。

## End-to-End Scenarios

- [ ] 场景 1：普通对话。验证：设置 `DEEPSEEK_API_KEY` 后运行 `python -m newcode`，输入普通问题，期望流式输出文字回复。
- [ ] 场景 2：读取文件。验证：输入“读取 pyproject.toml 并总结项目依赖”，期望 NewCode 调用读文件能力，并基于文件内容回复。
- [ ] 场景 3：搜索代码。验证：输入“搜索 ProviderError 在项目中的使用位置”，期望 NewCode 返回相关文件和行信息，并总结结果。
- [ ] 场景 4：唯一匹配修改文件。验证：准备一个临时测试文件，输入要求替换其中唯一一段文本，期望文件被正确修改。
- [ ] 场景 5：非唯一匹配修改失败。验证：准备一个包含重复文本的临时测试文件，要求替换重复文本，期望 NewCode 不修改文件，并说明匹配多次。
- [ ] 场景 6：执行命令。验证：输入“运行 python -m pytest tests/test_session.py 并总结结果”，期望返回命令退出状态和测试摘要。
- [ ] 场景 7：命令超时。验证：使用测试或受控命令触发超时，期望返回超时失败结果，不导致 CLI 卡死。
- [ ] 场景 8：路径越界。验证：请求读取工作区外文件，期望工具返回路径不允许错误，程序继续运行。
- [ ] 场景 9：干净退出。验证：输入 `/exit`，期望 NewCode 正常返回终端，无 traceback。
- [ ] 场景 10：tmux 可用性记录。验证：运行 `tmux -V`；如果不可用，在验收报告中说明原因并给出手动命令。
