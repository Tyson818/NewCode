# Newcode 最小对话闭环验收清单

> 每一项都必须通过运行命令或观察行为来验证。

## 实现完整性
- [ ] CLI 能启动交互式对话提示。（验证方式：在配置和环境变量齐全时运行 `python -m newcode`；观察终端出现启动提示和输入提示。）
- [ ] 用户输入非空消息后会触发一次模型请求。（验证方式：在测试中使用假 Provider 运行 CLI；输入一条消息；预期 Provider 只收到一次调用，并且调用内容包含该用户消息。）
- [ ] 助手回复会以流式方式逐步输出。（验证方式：运行 `python -m pytest tests/test_cli.py -k "stream"`；预期输出片段按 Provider yield 的顺序出现，并且发生在本轮结束前。）
- [ ] 完整助手回复会追加到当前会话历史。（验证方式：运行 `python -m pytest tests/test_cli.py -k "history"`；预期 user 和 assistant 消息按顺序保留。）
- [ ] 多轮对话会携带之前的 user 和 assistant 历史。（验证方式：运行 `python -m pytest tests/test_session.py tests/test_cli.py -k "history"`；预期第二次 Provider 调用包含第一轮完整对话。）
- [ ] 每次成功回复后，CLI 会回到下一轮输入提示。（验证方式：运行 CLI 循环测试；预期流式回复结束后再次出现输入提示。）
- [ ] 空输入会被忽略，不会触发模型请求。（验证方式：运行 `python -m pytest tests/test_cli.py -k "empty"`；预期 Provider 调用次数不变。）
- [ ] 明确退出输入会干净结束会话。（验证方式：运行 `python -m pytest tests/test_cli.py -k "exit"`；预期 `/exit`、`/quit` 和 `exit` 都能成功退出。）
- [ ] EOF 和键盘中断会干净退出。（验证方式：运行 `python -m pytest tests/test_cli.py -k "eof or interrupt"`；预期无 traceback，并表现为成功退出。）

## 配置与凭据
- [ ] `config.yaml` 包含 `model: deepseek-chat`。（验证方式：检查 `config.yaml`；预期默认模型值严格为 `deepseek-chat`。）
- [ ] `config.yaml` 包含 `base_url: https://api.deepseek.com`。（验证方式：检查 `config.yaml`；预期默认服务地址严格为 `https://api.deepseek.com`。）
- [ ] `config.yaml` 包含 `api_key_env: DEEPSEEK_API_KEY`，且不包含明文 API Key。（验证方式：运行 `Select-String -Path config.yaml -Pattern "sk-|api_key:"`；预期不匹配明文密钥或 `api_key` 字段。）
- [ ] YAML 配置可以加载有效值，并拒绝无效值。（验证方式：运行 `python -m pytest tests/test_config.py`；预期有效配置通过，缺失、空值或类型无效字段会触发 `ConfigError`。）
- [ ] API Key 只从配置指定的环境变量读取。（验证方式：运行 `python -m pytest tests/test_config.py -k "api_key"`；预期环境变量缺失或为空时失败，环境变量有效时成功。）
- [ ] 启动时配置或凭据错误会阻止模型请求。（验证方式：运行 `python -m pytest tests/test_cli.py -k "config or startup"`；预期返回非零退出码，并且没有 Provider 调用。）
- [ ] 面向终端的配置和凭据错误使用中文展示，且不会打印密钥值。（验证方式：使用假密钥运行相关测试；预期输出包含中文错误信息，且不包含该假密钥。）

## Provider 集成
- [ ] Provider 抽象暴露同步文本流接口。（验证方式：运行 `python -m compileall newcode/providers`；预期 Provider 模块编译成功。）
- [ ] DeepSeek Provider 使用配置的 `base_url` 和解析出的 API Key 初始化 OpenAI SDK。（验证方式：运行 `python -m pytest tests/test_deepseek_provider.py -k "client or base_url"`；预期 mock client 收到配置值。）
- [ ] DeepSeek Provider 使用配置的 `model`、完整消息历史和 `stream=True` 发起 Chat Completions 请求。（验证方式：运行 `python -m pytest tests/test_deepseek_provider.py -k "request"`；预期 mock 请求参数匹配。）
- [ ] DeepSeek Provider 只 yield 流式响应中的非空文本增量。（验证方式：运行 `python -m pytest tests/test_deepseek_provider.py -k "stream"`；预期输出片段与 mock 的非空 delta 一致。）
- [ ] Provider 失败会转换为 `ProviderError`。（验证方式：运行 `python -m pytest tests/test_deepseek_provider.py -k "error"`；预期 SDK 或网络异常被转换。）
- [ ] CLI 在 `ProviderError` 后可以恢复。（验证方式：运行 `python -m pytest tests/test_cli.py -k "provider_error or recover"`；预期显示中文错误、不追加 assistant 历史，并再次出现输入提示。）

## 范围边界
- [ ] 本阶段不存在 Agent 工具执行行为。（验证方式：运行自动化 CLI 测试，让用户文本请求文件、shell 或 Git 操作；预期文本仅作为普通 user 消息传给 Provider，不触发任何本地命令或工具路径。）
- [ ] Newcode 不暴露 shell 命令执行功能。（验证方式：运行 `rg "subprocess|os\\.system|Popen|shell=True" newcode`；预期运行时代码中没有匹配。）
- [ ] Newcode 不暴露 Git 操作功能。（验证方式：运行 `rg "\\bgit\\b" newcode`；预期没有运行时命令路径或 Git 集成代码。）
- [ ] 未实现文件编辑或 RAG 行为。（验证方式：运行越界场景 CLI 测试；预期 Newcode 不检查工作区文件，只输出 Provider 返回文本。）
- [ ] 不持久化长期记忆。（验证方式：运行测试或手动重启；预期进程重启后对话历史为空。）

## 构建与测试
- [ ] 项目元数据声明运行依赖 `openai` 和 `PyYAML`。（验证方式：检查 `pyproject.toml`；预期两个依赖都已声明。）
- [ ] 项目元数据声明测试依赖 `pytest`。（验证方式：检查 `pyproject.toml`；预期 `pytest` 已作为测试依赖声明。）
- [ ] Python 包能通过语法编译检查。（验证方式：运行 `python -m compileall newcode`；预期命令成功。）
- [ ] 单元测试通过。（验证方式：运行 `python -m pytest`；预期所有测试通过。）
- [ ] 自动化测试不依赖真实 DeepSeek API Key 或网络。（验证方式：在未设置 `DEEPSEEK_API_KEY` 的情况下运行 `python -m pytest`；预期所有自动化测试仍然通过。）
- [ ] 如果 pytest 不可用，需要记录 unittest fallback。（验证方式：运行 `python -m unittest discover`；预期记录运行结果，并说明 pytest 无法运行的原因。）
- [ ] 未配置 lint 工具时，不要求 lint。（验证方式：检查 `pyproject.toml`；如果没有配置 lint 工具，则记录 lint 不适用。）

## 端到端场景
- [ ] 场景 1：真实启动并退出。（验证方式：设置 `DEEPSEEK_API_KEY` 后运行 `python -m newcode`，观察输入提示，输入 `/exit`；预期干净返回终端。）
- [ ] 场景 2：真实单轮流式回复。（验证方式：在 tmux 中启动 `python -m newcode`，输入 `用一句话介绍 Newcode`；预期回复文本逐步出现，并在完成后回到输入提示。）
- [ ] 场景 3：真实多轮上下文。（验证方式：在同一个 tmux 会话中输入 `我叫小明，只回答收到`，等待回复后输入 `我叫什么？`；预期回复能使用本会话中的名字。）
- [ ] 场景 4：模型请求失败后可恢复。（验证方式：使用无效 API Key 或故意设置无效模型后，输入一个普通问题，观察中文错误，再输入 `/exit`；预期无 traceback 并干净退出。）
- [ ] 场景 5：缺少凭据时启动失败。（验证方式：取消设置 `DEEPSEEK_API_KEY` 后运行 `python -m newcode`；预期显示清晰中文错误，并且不会进入可发起模型请求的交互状态。）
- [ ] 场景 6：终端中断处理。（验证方式：启动 `python -m newcode`，按 Ctrl+C 或发送 tmux interrupt；预期无 traceback，并干净返回终端。）
- [ ] 场景 7：越界请求仍然只是普通对话。（验证方式：询问 `请执行 git status 并修改文件`；预期 Newcode 只输出模型回复，不执行 Git、shell、文件编辑、tool use、RAG、记忆或 Agent loop 行为。）
- [ ] 场景 8：记录 tmux 可用性。（验证方式：运行 `tmux -V`；如果当前环境不可用，需要记录原因，并给出手动命令 `python -m newcode`、`用一句话介绍 Newcode`、`我叫小明，只回答收到`、`我叫什么？`、`/exit`。）
