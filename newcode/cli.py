from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TextIO

from newcode.config import ConfigError, load_config, resolve_api_key
from newcode.providers.base import ChatProvider, ProviderError, ProviderEvent, TextDelta, ToolCallEvent
from newcode.providers.deepseek import DeepSeekProvider
from newcode.session import ChatMessage, ChatSession
from newcode.tools.executor import execute_tool_call, make_failure_result
from newcode.tools.registry import ToolRegistry, create_default_registry
from newcode.tools.types import ToolCall, ToolContext, ToolResult


DEFAULT_CONFIG_PATH = Path("config.yaml")
EXIT_COMMANDS = {"/exit", "/quit", "exit"}
FINAL_ANSWER_INSTRUCTION = (
    "你现在处于最终回答阶段。工具已经执行完毕。"
    "禁止再次调用工具，禁止输出 DSML/tool_calls 标记。"
    "请只根据上一条 tool 消息中的工具执行结果，用自然语言回答用户。"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="newcode")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="配置文件路径，默认 config.yaml",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
        api_key = resolve_api_key(config.api_key_env)
        provider = DeepSeekProvider(config=config, api_key=api_key)
        registry = create_default_registry()
        tool_context = ToolContext(
            workspace_root=Path(config.workspace_root),
            default_timeout_seconds=config.tool_timeout_seconds,
            command_timeout_seconds=config.command_timeout_seconds,
            sensitive_values=(api_key,),
        )
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    return run_conversation(
        provider=provider,
        session=ChatSession(),
        registry=registry,
        tool_context=tool_context,
    )


def run_conversation(
    provider: ChatProvider,
    session: ChatSession,
    *,
    registry: ToolRegistry | None = None,
    tool_context: ToolContext | None = None,
    input_func: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
    error_output: TextIO = sys.stderr,
) -> int:
    registry = registry or create_default_registry()
    tool_context = tool_context or ToolContext(workspace_root=Path.cwd())
    print("NewCode 已启动。输入问题开始对话，输入 /exit 退出。", file=output)

    while True:
        try:
            user_input = input_func("你> ")
        except EOFError:
            print("\n已结束对话。", file=output)
            return 0
        except KeyboardInterrupt:
            print("\n已中断对话。", file=output)
            return 0

        text = user_input.strip()
        if not text:
            continue
        if text in EXIT_COMMANDS:
            print("已结束对话。", file=output)
            return 0

        session.add_user_message(text)
        print("NewCode> ", end="", file=output, flush=True)

        try:
            first_response = _consume_provider_events(
                provider.stream_chat(
                    session.messages,
                    tools=registry.to_openai_tools(),
                    allow_tool_calls=True,
                ),
                output=output,
            )
        except ProviderError as exc:
            print("", file=output)
            print(f"模型错误：{exc.message}", file=error_output)
            continue

        if first_response.tool_calls:
            _handle_tool_calls(
                first_response.tool_calls,
                session=session,
                registry=registry,
                tool_context=tool_context,
            )
            try:
                final_messages = [
                    *session.messages,
                    ChatMessage(role="user", content=FINAL_ANSWER_INSTRUCTION),
                ]
                final_response = _consume_provider_events(
                    provider.stream_chat(
                        final_messages,
                        tools=None,
                        allow_tool_calls=False,
                    ),
                    output=output,
                )
            except ProviderError as exc:
                print("", file=output)
                print(f"模型错误：{exc.message}", file=error_output)
                continue

            if final_response.tool_calls:
                message = "模型在最终回复阶段继续请求工具，本阶段不会继续执行。"
                print(message, end="", file=output, flush=True)
                session.add_assistant_message(message)
            elif final_response.text.strip():
                session.add_assistant_message(final_response.text)
            print("", file=output)
            continue

        if first_response.text.strip():
            session.add_assistant_message(first_response.text)
        print("", file=output)


class _ModelResponse:
    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.tool_calls: list[ToolCall] = []

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


def _consume_provider_events(
    events: Iterable[ProviderEvent],
    *,
    output: TextIO,
) -> _ModelResponse:
    response = _ModelResponse()
    for event in events:
        if isinstance(event, TextDelta):
            response.text_parts.append(event.text)
            print(event.text, end="", file=output, flush=True)
        elif isinstance(event, ToolCallEvent):
            response.tool_calls.extend(event.tool_calls)
    return response


def _handle_tool_calls(
    tool_calls: list[ToolCall],
    *,
    session: ChatSession,
    registry: ToolRegistry,
    tool_context: ToolContext,
) -> None:
    session.add_assistant_tool_calls(tool_calls)
    if len(tool_calls) != 1:
        for tool_call in tool_calls:
            session.add_tool_result(
                tool_call.id,
                make_failure_result(
                    tool_call.name,
                    "multiple_tool_calls_not_supported",
                    "本阶段每轮只支持一次工具调用",
                    {"requested_count": len(tool_calls)},
                    context=tool_context,
                ),
            )
        return

    tool_call = tool_calls[0]
    if tool_call.name == "__tool_call_parse_error__":
        result = make_failure_result(
            tool_call.name,
            "tool_call_parse_error",
            "模型输出的工具参数不是有效 JSON 对象",
            {"raw_arguments": tool_call.raw_arguments},
            context=tool_context,
        )
    else:
        result = execute_tool_call(tool_call, registry, tool_context)
    session.add_tool_result(tool_call.id, result)
