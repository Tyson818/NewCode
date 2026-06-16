from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from mewcode.config import ConfigError, load_config, resolve_api_key
from mewcode.providers.base import ChatProvider, ProviderError
from mewcode.providers.deepseek import DeepSeekProvider
from mewcode.session import ChatSession


DEFAULT_CONFIG_PATH = Path("config.yaml")
EXIT_COMMANDS = {"/exit", "/quit", "exit"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mewcode")
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
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    return run_conversation(provider=provider, session=ChatSession())


def run_conversation(
    provider: ChatProvider,
    session: ChatSession,
    *,
    input_func: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
    error_output: TextIO = sys.stderr,
) -> int:
    print("MewCode 已启动。输入问题开始对话，输入 /exit 退出。", file=output)

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
        chunks: list[str] = []
        print("MewCode> ", end="", file=output, flush=True)
        try:
            for chunk in provider.stream_chat(session.messages):
                chunks.append(chunk)
                print(chunk, end="", file=output, flush=True)
        except ProviderError as exc:
            print("", file=output)
            print(f"模型错误：{exc.message}", file=error_output)
            continue

        assistant_message = "".join(chunks)
        if assistant_message.strip():
            session.add_assistant_message(assistant_message)
        print("", file=output)
