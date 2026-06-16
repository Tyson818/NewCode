# Newcode Minimal Conversation Loop Tasks

## File List

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `pyproject.toml` | 声明项目元数据、运行入口、运行依赖和测试配置 |
| Create | `config.yaml` | 提供默认 YAML 配置示例，不包含真实 API Key |
| Create | `newcode/__init__.py` | 标识 Python 包和基础版本信息 |
| Create | `newcode/__main__.py` | 支持 `python -m newcode` 启动 |
| Create | `newcode/cli.py` | 终端输入循环、流式打印、退出和错误展示 |
| Create | `newcode/config.py` | YAML 配置加载、默认值合并、配置校验和环境变量凭据读取 |
| Create | `newcode/session.py` | 会话消息结构和多轮历史管理 |
| Create | `newcode/providers/__init__.py` | Provider 包导出 |
| Create | `newcode/providers/base.py` | Provider 协议和统一异常 |
| Create | `newcode/providers/deepseek.py` | DeepSeek OpenAI-compatible 流式 Provider |
| Create | `tests/test_config.py` | 配置加载和凭据错误测试 |
| Create | `tests/test_session.py` | 多轮消息历史测试 |
| Create | `tests/test_cli.py` | CLI 输入循环、退出、错误和流式输出测试 |
| Create | `tests/test_deepseek_provider.py` | DeepSeek Provider 请求参数和异常转换测试 |

## T1: 创建项目骨架和包入口占位

**Files:** `pyproject.toml`, `newcode/__init__.py`, `newcode/__main__.py`, `newcode/providers/__init__.py`

**Depends On:** None

**Steps:**
1. 创建基础 Python 包目录和 Provider 子包目录。
2. 在 `pyproject.toml` 中声明项目名、Python 版本要求、运行入口和依赖范围。
3. 声明运行依赖 `openai` 和 `PyYAML`。
4. 明确声明测试依赖 `pytest`。
5. 在 `__main__.py` 中保留调用 CLI 入口的最小结构。

**Validation:** Run `python -m compileall newcode`; expect all created package files compile without syntax errors.

## T2: 创建默认 YAML 配置示例

**Files:** `config.yaml`

**Depends On:** T1

**Steps:**
1. 添加默认 `model`、`base_url` 和 `api_key_env` 字段。
2. 设置默认 `model` 为 `deepseek-chat`。
3. 设置默认 `base_url` 为 `https://api.deepseek.com`。
4. 设置默认 `api_key_env` 为 `DEEPSEEK_API_KEY`。
5. 确认配置文件不包含真实 API Key。

**Validation:** Run `Select-String -Path config.yaml -Pattern "sk-|api_key:|DEEPSEEK_API_KEY"`; expect only `DEEPSEEK_API_KEY` appears and no literal API key field appears.

## T3: 实现会话历史结构

**Files:** `newcode/session.py`, `tests/test_session.py`

**Depends On:** T1

**Steps:**
1. 定义 `ChatMessage`，只允许当前阶段需要的 `user` 和 `assistant` 角色。
2. 定义 `ChatSession`，按顺序保存当前进程内消息。
3. 添加用户消息和助手消息追加行为。
4. 添加转换为 Provider 请求消息的行为。
5. 编写测试覆盖空初始历史、追加顺序和转换结果。

**Validation:** Run `python -m pytest tests/test_session.py`; expect all session tests pass.

## T4: 实现 Provider 基础接口

**Files:** `newcode/providers/base.py`, `newcode/providers/__init__.py`

**Depends On:** T1, T3

**Steps:**
1. 定义 `ChatProvider` 协议，暴露同步 `stream_chat(...) -> Iterator[str]`。
2. 定义 `ProviderError`，用于统一模型服务层错误。
3. 在 Provider 包中导出基础接口和异常。

**Validation:** Run `python -m compileall newcode/providers`; expect provider package compiles without syntax errors.

## T5: 实现配置加载和凭据解析

**Files:** `newcode/config.py`, `tests/test_config.py`

**Depends On:** T1, T2

**Steps:**
1. 定义 `AppConfig` 和 `ConfigError`。
2. 实现从 YAML 文件读取配置。
3. 合并默认 `base_url` 和 `api_key_env`。
4. 校验 `model`、`base_url`、`api_key_env` 必须非空且类型正确。
5. 实现只从指定环境变量读取 API Key。
6. 编写测试覆盖正常配置、缺失文件、无效字段、缺失环境变量和空环境变量。

**Validation:** Run `python -m pytest tests/test_config.py`; expect all config tests pass.

## T6: 实现 DeepSeek Provider 流式调用

**Files:** `newcode/providers/deepseek.py`, `tests/test_deepseek_provider.py`

**Depends On:** T3, T4, T5

**Steps:**
1. 使用 `AppConfig` 和 API Key 初始化 OpenAI SDK 客户端。
2. 将 `ChatMessage` 历史转换为 OpenAI-compatible Chat Completions 消息。
3. 使用配置中的模型名和服务地址发起 `stream=True` 请求。
4. 从流式响应中提取非空文本片段并逐个 yield。
5. 将 SDK 或网络异常转换为 `ProviderError`。
6. 编写测试通过 mock 验证请求参数、流式片段输出和异常转换。

**Validation:** Run `python -m pytest tests/test_deepseek_provider.py`; expect all DeepSeek Provider tests pass without real network requests.

## T7: 实现 CLI 启动和配置错误路径

**Files:** `newcode/cli.py`, `newcode/__main__.py`, `tests/test_cli.py`

**Depends On:** T4, T5, T6

**Steps:**
1. 实现 `main(argv=None)`，加载默认配置文件。
2. 在启动时解析 API Key，并创建 DeepSeek Provider 和 ChatSession。
3. 配置错误或凭据错误时打印中文错误并返回非零退出码。
4. 确保 `python -m newcode` 调用 `main()`。
5. 编写测试覆盖配置错误时不会创建 Provider 或发起请求。

**Validation:** Run `python -m pytest tests/test_cli.py -k "config or startup"`; expect startup and config error tests pass.

## T8: 实现终端对话循环和退出处理

**Files:** `newcode/cli.py`, `tests/test_cli.py`

**Depends On:** T3, T4, T7

**Steps:**
1. 实现启动提示和输入提示。
2. 忽略空白输入并重新提示。
3. 支持 `/exit`、`/quit` 和 `exit` 结束会话。
4. 处理 EOF 和键盘中断并干净退出。
5. 编写测试覆盖普通退出、空输入、EOF 和键盘中断。

**Validation:** Run `python -m pytest tests/test_cli.py -k "exit or empty or eof or interrupt"`; expect loop control tests pass.

## T9: 实现流式打印和历史更新

**Files:** `newcode/cli.py`, `tests/test_cli.py`

**Depends On:** T3, T4, T8

**Steps:**
1. 用户输入非空消息后追加到会话历史。
2. 调用 Provider 并将每个文本片段立即打印到终端。
3. 收集完整助手回复，并在成功结束后追加到会话历史。
4. 一轮结束后打印清晰换行并回到下一轮输入提示。
5. 编写测试使用假 Provider 验证流式片段顺序、flush 行为和历史内容。

**Validation:** Run `python -m pytest tests/test_cli.py -k "stream or history"`; expect streaming and history tests pass.

## T10: 实现模型请求失败后的恢复

**Files:** `newcode/cli.py`, `tests/test_cli.py`

**Depends On:** T4, T8, T9

**Steps:**
1. 捕获 Provider 抛出的 `ProviderError`。
2. 以中文展示错误，不输出 API Key 或敏感底层信息。
3. 请求失败时不追加助手消息。
4. 错误后回到下一轮输入提示，允许用户继续输入或退出。
5. 编写测试覆盖失败恢复和历史不污染。

**Validation:** Run `python -m pytest tests/test_cli.py -k "provider_error or recover"`; expect provider failure recovery tests pass.

## T11: 运行全量自动化验证

**Files:** `pyproject.toml`, `newcode/**`, `tests/**`

**Depends On:** T1, T2, T3, T4, T5, T6, T7, T8, T9, T10

**Steps:**
1. 运行 Python 语法编译检查。
2. 运行完整 pytest 测试套件。
3. 若 pytest 不可用，按项目规则改用 `python -m unittest discover` 并记录原因。
4. 确认自动化测试不依赖真实 DeepSeek API Key 或网络。

**Validation:** Run `python -m compileall newcode` and `python -m pytest`; expect both commands pass.

## Execution Order

```text
T1 -> T2 -> T5 ----\
 |      \           \
 |       -> T3 -> T4 -> T6 -> T7 -> T8 -> T9 -> T10 -> T11
 |                  /
 +-----------------/
```

## Traceability

| Plan Component | Tasks |
|----------------|-------|
| Project packaging and startup | T1, T7, T11 |
| YAML configuration and API Key resolution | T2, T5, T7 |
| Session history | T3, T9 |
| Provider abstraction | T4 |
| DeepSeek OpenAI-compatible Provider | T6 |
| CLI conversation loop | T7, T8, T9, T10 |
| Automated tests | T3, T5, T6, T7, T8, T9, T10, T11 |

## Acceptance Coverage Preview

| Spec Acceptance | Covered By |
|-----------------|------------|
| AC1 | T7, T8, T11 |
| AC2 | T6, T9, T11 |
| AC3 | T6, T9, T11 |
| AC4 | T3, T9, T11 |
| AC5 | T8, T9, T11 |
| AC6 | T8, T11 |
| AC7 | T5, T7, T11 |
| AC8 | T10, T11 |
| AC9 | T2, T5, T7, T11 |
| AC10 | T4, T6, T9, T11 |
| AC11 | T11 and the later `checklist.md` end-to-end checks |
