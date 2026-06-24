from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from newcode.agent import (
    AgentFinalAnswer,
    AgentLoop,
    AgentStopped,
    AgentTextDelta,
    AgentToolCallStarted,
    AgentToolError,
    AgentUsage,
    StopReason,
)
from newcode.agent.mode import AgentMode
from newcode.config import ConfigError, load_config, resolve_api_key
from newcode.providers.base import ChatProvider
from newcode.providers.deepseek import DeepSeekProvider
from newcode.session import ChatSession
from newcode.tools.registry import ToolRegistry, create_default_registry
from newcode.tools.types import ToolContext


DEFAULT_CONFIG_PATH = Path("config.yaml")
EXIT_COMMANDS = {"/exit", "/quit", "exit"}
PLAN_COMMAND = "/plan"
DO_COMMAND = "/do"


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
    mode = AgentMode.DO

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
        if text == PLAN_COMMAND:
            mode = AgentMode.PLAN
            print("已切换到 Plan Mode。", file=output)
            continue
        if text == DO_COMMAND:
            mode = AgentMode.DO
            print("已切换到 Do Mode。", file=output)
            continue

        print("NewCode> ", end="", file=output, flush=True)
        loop = AgentLoop(
            provider=provider,
            session=session,
            registry=registry,
            tool_context=tool_context,
        )

        try:
            _consume_agent_events(
                loop.run(text, mode=mode),
                output=output,
                error_output=error_output,
            )
        except KeyboardInterrupt:
            print("\n已中断当前任务。", file=output)
        print("", file=output)


def _consume_agent_events(
    events,
    *,
    output: TextIO,
    error_output: TextIO,
) -> None:
    printed_text = False
    for event in events:
        if isinstance(event, AgentTextDelta):
            printed_text = True
            print(event.text, end="", file=output, flush=True)
            continue

        if isinstance(event, AgentToolCallStarted):
            print(
                f"\n[工具] {event.tool_call.name}",
                file=output,
                flush=True,
            )
            continue

        if isinstance(event, AgentToolError):
            print(
                f"\n[工具错误] {event.code}: {event.message}",
                file=output,
                flush=True,
            )
            continue

        if isinstance(event, AgentStopped):
            if event.reason is StopReason.PROVIDER_ERROR:
                print(f"模型错误：{event.message}", file=error_output)
            else:
                print(f"\n已停止：{event.message}", file=output, flush=True)
            continue

        if isinstance(event, AgentFinalAnswer):
            if event.content and not printed_text:
                print(event.content, end="", file=output, flush=True)
            continue

        if isinstance(event, AgentUsage):
            continue
