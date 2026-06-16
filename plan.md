# Newcode Minimal Conversation Loop Plan

## Architecture Overview
Newcode 本阶段采用分层结构，保持纯对话闭环的最小范围：

- CLI 层负责终端启动、输入循环、退出处理、流式打印和中文错误展示。
- Session 层负责维护当前进程内的多轮消息历史。
- Provider 层定义统一模型服务接口，并提供 DeepSeek 的 OpenAI-compatible 实现。
- Config 层负责读取 YAML 配置、校验必要字段，并从环境变量读取 API Key。
- Test 层使用可替换的假 Provider 验证终端交互、历史传递、流式输出和错误路径。

运行时数据流：

```text
用户终端输入
    -> CLI 输入循环
    -> Session 追加 user 消息
    -> Provider 以完整历史发起流式对话
    -> CLI 逐块打印回复
    -> Session 追加 assistant 完整回复
    -> CLI 回到下一轮输入提示
```

## Core Data Structures

### AppConfig
保存运行所需配置。

字段：
- `model: str`：对话模型名称。
- `base_url: str`：OpenAI-compatible 服务地址，默认 `https://api.deepseek.com`。
- `api_key_env: str`：API Key 所在环境变量名，默认 `DEEPSEEK_API_KEY`。

约束：
- `model`、`base_url`、`api_key_env` 必须存在且非空。
- API Key 不进入该结构，只在创建 Provider 时从环境变量读取。

### ChatMessage
表示一条 Chat Completions 消息。

字段：
- `role: Literal["user", "assistant"]`：消息角色。
- `content: str`：消息正文。

约束：
- 当前阶段不加入 `system`、`tool`、`developer` 等角色。
- 空白用户输入不进入历史。

### ChatSession
保存当前进程内的会话历史。

字段：
- `messages: list[ChatMessage]`：按时间顺序保存的 user / assistant 消息。

行为：
- 追加用户消息。
- 追加助手回复。
- 返回适配模型服务请求的完整历史。

### ProviderError
统一表示模型服务层错误。

字段：
- `message: str`：可展示给用户的中文错误摘要。
- `cause: Exception | None`：底层异常，仅用于调试和测试，不向终端暴露敏感信息。

## Core Interfaces

### ChatProvider
Provider 抽象接口。

签名：
```python
class ChatProvider(Protocol):
    def stream_chat(self, messages: Sequence[ChatMessage]) -> Iterator[str]:
        ...
```

职责：
- 接收完整会话历史。
- 返回逐步生成的文本片段。
- 对外抛出统一的 `ProviderError`。

### ConfigLoader
配置加载接口。

签名：
```python
def load_config(path: Path) -> AppConfig:
    ...
```

职责：
- 从 YAML 文件读取配置。
- 合并默认值。
- 校验必填项和基本类型。
- 对配置缺失或无效给出可展示的中文错误。

### ApiKeyResolver
凭据读取接口。

签名：
```python
def resolve_api_key(env_name: str) -> str:
    ...
```

职责：
- 只从指定环境变量读取 API Key。
- 缺失或空值时返回清晰错误。
- 不打印、不记录、不保存 API Key。

### ConversationRunner
终端会话编排接口。

签名：
```python
def run_conversation(provider: ChatProvider, session: ChatSession) -> int:
    ...
```

职责：
- 显示启动提示和输入提示。
- 读取用户输入。
- 处理空输入、退出输入、EOF 和键盘中断。
- 调用 Provider 并逐块打印流式回复。
- 成功回复后更新助手历史。
- 请求失败时显示错误并保留用户可继续输入的状态。

## Module Design

### `newcode.cli`
**Responsibility:** 程序入口和终端交互编排。

**Public Interface:**
- `main(argv: list[str] | None = None) -> int`
- `run_conversation(provider: ChatProvider, session: ChatSession) -> int`

**Dependencies:** `newcode.config`、`newcode.session`、`newcode.providers.deepseek`、`newcode.providers.base`

**Spec Ownership:** F1、F2、F3、F5、F6、F7、F8

### `newcode.config`
**Responsibility:** YAML 配置读取、默认值合并、配置校验、API Key 环境变量解析。

**Public Interface:**
- `AppConfig`
- `ConfigError`
- `load_config(path: Path) -> AppConfig`
- `resolve_api_key(env_name: str) -> str`

**Dependencies:** `pathlib`、`os`、`yaml`

**Spec Ownership:** F7、F9

### `newcode.session`
**Responsibility:** 当前进程内的多轮会话历史管理。

**Public Interface:**
- `ChatMessage`
- `ChatSession`
- `ChatSession.add_user_message(content: str) -> None`
- `ChatSession.add_assistant_message(content: str) -> None`
- `ChatSession.to_provider_messages() -> list[dict[str, str]]`

**Dependencies:** 标准库类型工具

**Spec Ownership:** F4、F5

### `newcode.providers.base`
**Responsibility:** Provider 抽象接口和统一异常。

**Public Interface:**
- `ChatProvider`
- `ProviderError`

**Dependencies:** 标准库类型工具

**Spec Ownership:** F10

### `newcode.providers.deepseek`
**Responsibility:** 使用 OpenAI SDK 调用 DeepSeek OpenAI-compatible Chat Completions，并提供流式文本片段。

**Public Interface:**
- `DeepSeekProvider(config: AppConfig, api_key: str)`
- `DeepSeekProvider.stream_chat(messages: Sequence[ChatMessage]) -> Iterator[str]`

**Dependencies:** `openai`、`newcode.config`、`newcode.session`、`newcode.providers.base`

**Spec Ownership:** F2、F3、F8、F10

### `newcode.__main__`
**Responsibility:** 支持 `python -m newcode` 启动。

**Public Interface:**
- 调用 `newcode.cli.main`

**Dependencies:** `newcode.cli`

**Spec Ownership:** F1

### `tests`
**Responsibility:** 覆盖配置、会话、Provider 边界和 CLI 行为。

**Public Interface:** 无运行时公开接口。

**Dependencies:** `pytest`

**Spec Ownership:** 所有验收标准的自动化覆盖基础；端到端手工验收在 `checklist.md` 细化。

## Module Interaction

### Startup
```text
python -m newcode
    -> newcode.__main__
    -> cli.main
    -> config.load_config
    -> config.resolve_api_key
    -> DeepSeekProvider
    -> ChatSession
    -> cli.run_conversation
```

### One Conversation Turn
```text
CLI 读取输入
    -> 输入为空：忽略并重新提示
    -> 输入为退出指令：结束进程
    -> 普通输入：ChatSession.add_user_message
    -> DeepSeekProvider.stream_chat(session.messages)
    -> CLI 将每个 chunk 立即 print(..., flush=True)
    -> CLI 收集完整 assistant 文本
    -> ChatSession.add_assistant_message
    -> CLI 打印下一轮提示
```

### Error Flow
```text
配置错误 / API Key 缺失
    -> main 显示中文错误
    -> 不创建 Provider
    -> 不发起模型请求
    -> 返回非零退出码

模型请求失败
    -> Provider 转换为 ProviderError
    -> CLI 显示中文错误
    -> 不追加 assistant 消息
    -> 回到下一轮输入提示
```

## File Organization

```text
newcode/
+-- pyproject.toml                  - 项目元数据、运行入口和依赖声明
+-- config.yaml                     - 默认本地配置示例，不包含 API Key
+-- newcode/
|   +-- __init__.py                 - 包标识和版本信息
|   +-- __main__.py                 - python -m newcode 入口
|   +-- cli.py                      - 终端输入循环、流式打印和错误展示
|   +-- config.py                   - YAML 配置加载和环境变量凭据读取
|   +-- session.py                  - 会话历史和消息结构
|   +-- providers/
|       +-- __init__.py             - Provider 包导出
|       +-- base.py                 - Provider 协议和统一异常
|       +-- deepseek.py             - DeepSeek OpenAI-compatible Provider
+-- tests/
    +-- test_config.py              - 配置和凭据错误测试
    +-- test_session.py             - 多轮历史测试
    +-- test_cli.py                 - 输入循环、退出、错误和流式输出测试
    +-- test_deepseek_provider.py   - Provider 请求参数和异常转换测试
```

说明：
- 当前仓库尚无 Python 包结构，因此阶段三会把这些文件列为新增文件。
- `config.yaml` 只保存 `model`、`base_url`、`api_key_env`，不保存真实 API Key。
- 测试中通过假 Provider 或 mock OpenAI SDK 行为验证，不依赖真实 DeepSeek 网络请求。

## Technical Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| CLI 启动方式 | 支持 `python -m newcode`，并在 `pyproject.toml` 中声明 `newcode` 命令 | 兼顾未安装时的直接运行和后续正式命令入口 |
| Provider 接口 | 使用同步 `Iterator[str]` 流式接口 | 终端最小闭环不需要异步运行时，测试和输出更直接 |
| DeepSeek 调用 | 使用 OpenAI SDK 的 Chat Completions，配置 `api_key` 和 `base_url`，请求开启 `stream=True` | 满足 OpenAI-compatible 和 DeepSeek API 约定，保留未来 Provider 替换空间 |
| 配置格式 | 使用 YAML，默认字段为 `model`、`base_url`、`api_key_env` | 符合 spec 对 YAML 配置的要求，字段最小 |
| YAML 解析 | 使用 `PyYAML` | Python 标准库不支持 YAML；这是实现 YAML 配置的窄依赖 |
| API Key 读取 | 只通过 `api_key_env` 指定的环境变量读取 | 避免密钥进入代码、配置和输出 |
| 会话历史 | 仅在 `ChatSession` 中以进程内列表保存 user / assistant 消息 | 满足多轮上下文，不引入持久化或长期记忆 |
| 错误处理 | 配置错误启动即失败；模型请求失败后回到输入循环 | 配置错误无法继续，运行中服务失败则允许用户重试或退出 |
| 退出指令 | 支持 `/exit`、`/quit` 和 `exit` | 提供明确退出输入，同时保持命令系统最小化 |
| 空输入 | 忽略并重新提示 | 避免发送无意义模型请求 |
| 测试策略 | 单元测试优先使用假 Provider 和 mock，不调用真实 API | 测试稳定、无网络和密钥依赖 |

## Requirements Coverage

| Requirement | Architectural Owner |
|-------------|---------------------|
| F1 | `newcode.cli`、`newcode.__main__` |
| F2 | `newcode.cli`、`newcode.providers.deepseek` |
| F3 | `newcode.cli`、`newcode.providers.deepseek` |
| F4 | `newcode.session` |
| F5 | `newcode.cli`、`newcode.session` |
| F6 | `newcode.cli` |
| F7 | `newcode.config`、`newcode.cli` |
| F8 | `newcode.providers.base`、`newcode.providers.deepseek`、`newcode.cli` |
| F9 | `newcode.config` |
| F10 | `newcode.providers.base`、`newcode.providers.deepseek` |
