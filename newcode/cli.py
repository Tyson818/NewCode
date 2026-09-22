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
from newcode.context.manager import ContextManager
from newcode.config import ConfigError, load_config, resolve_api_key
from newcode.mcp.adapter import MCPToolAdapter
from newcode.mcp.config import load_mcp_config
from newcode.mcp.manager import MCPManager
from newcode.mcp.naming import MCPToolSchemaError, validate_input_schema
from newcode.mcp.runtime import MCPRuntime
from newcode.permissions.confirmer import CliPermissionConfirmer, PermissionConfirmer
from newcode.permissions.loader import PermissionRulesLoadResult, load_permission_rules
from newcode.permissions.manager import PermissionManager
from newcode.permissions.types import PermissionMode
from newcode.providers.base import ChatProvider
from newcode.providers.deepseek import DeepSeekProvider
from newcode.memory.service import MemoryGenerationRequest, MemoryService
from newcode.memory.store import MemoryStore
from newcode.persistence import SessionArchive, SessionArchiveError
from newcode.session import ChatMessage, ChatSession
from newcode.tools.registry import ToolRegistry, create_default_registry
from newcode.tools.types import ToolContext


DEFAULT_CONFIG_PATH = Path("config.yaml")
EXIT_COMMANDS = {"/exit", "/quit", "exit"}
PLAN_COMMAND = "/plan"
DO_COMMAND = "/do"
COMPACT_COMMAND = "/compact"
SESSIONS_COMMAND = "/sessions"
RESUME_COMMAND = "/resume"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="newcode")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="配置文件路径，默认 config.yaml",
    )
    args = parser.parse_args(argv)

    mcp_runtime = None
    mcp_manager = None
    memory_service = None
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
        permission_rules = load_permission_rules(tool_context.workspace_root)
        mcp_config = load_mcp_config(tool_context.workspace_root)
        mcp_runtime = MCPRuntime()
        mcp_manager = MCPManager(mcp_config.servers.values(), runtime=mcp_runtime)
        for name, error in mcp_config.errors.items():
            print(f"MCP server unavailable ({name}): {error.code}", file=sys.stderr)
        for name, descriptors in mcp_manager.discover_all().items():
            config_entry = mcp_config.servers[name]
            for descriptor in descriptors:
                try:
                    validate_input_schema(descriptor.input_schema)
                    adapter = MCPToolAdapter(mcp_manager, config_entry, descriptor)
                    registry.register(adapter, read_only=False, do_visible=True)
                except (MCPToolSchemaError, ValueError):
                    print(f"MCP tool unavailable ({name}): mcp_tool_schema_invalid", file=sys.stderr)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    try:
        session_archive = SessionArchive(
            tool_context.workspace_root,
            sensitive_values=tool_context.sensitive_values,
        )
        memory_service = MemoryService(
            MemoryStore(
                tool_context.workspace_root,
                sensitive_values=tool_context.sensitive_values,
            ),
            lambda request: _generate_memory(provider, request),
            sensitive_values=tool_context.sensitive_values,
        )
        return run_conversation(
            provider=provider,
            session=ChatSession(),
            registry=registry,
            tool_context=tool_context,
            permission_mode=config.permission_mode,
            permission_rules=permission_rules,
            session_archive=session_archive,
            memory_service=memory_service,
        )
    finally:
        if memory_service is not None:
            _safe_memory_shutdown(memory_service)
        if mcp_manager is not None:
            mcp_manager.shutdown()
        if mcp_runtime is not None:
            mcp_runtime.shutdown()


def run_conversation(
    provider: ChatProvider,
    session: ChatSession,
    *,
    registry: ToolRegistry | None = None,
    tool_context: ToolContext | None = None,
    input_func: Callable[[str], str] = input,
    output: TextIO = sys.stdout,
    error_output: TextIO = sys.stderr,
    permission_manager: PermissionManager | None = None,
    permission_confirmer: PermissionConfirmer | None = None,
    permission_mode: PermissionMode = PermissionMode.DEFAULT,
    permission_rules: PermissionRulesLoadResult | None = None,
    context_manager: ContextManager | None = None,
    session_archive: SessionArchive | None = None,
    memory_service: MemoryService | None = None,
) -> int:
    registry = registry or create_default_registry()
    tool_context = tool_context or ToolContext(workspace_root=Path.cwd())
    permission_manager = permission_manager or _build_permission_manager(
        mode=permission_mode,
        rules=permission_rules,
        confirmer=permission_confirmer
        or CliPermissionConfirmer(input_func=input_func, output=output),
    )
    if session_archive is not None:
        _safe_cleanup_stale(session_archive, session.session_id)
        if session.session_id is None:
            _safe_create_session(session_archive, session)
    context_manager = context_manager or ContextManager(
        session,
        tool_context.workspace_root,
        tool_context.sensitive_values,
    )
    mode = AgentMode.DO

    print("NewCode 已启动。输入问题开始对话，输入 /exit 退出。", file=output)
    try:
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
        if text == SESSIONS_COMMAND:
            _print_sessions(session_archive, output)
            continue
        if text.startswith(RESUME_COMMAND):
            restored = _resume_session(session_archive, text)
            if restored is None:
                print("会话不可恢复。", file=output)
                continue
            _safe_checkpoint(session_archive, session)
            _safe_context_cleanup(context_manager)
            session = restored.session
            context_manager = ContextManager(
                session,
                tool_context.workspace_root,
                tool_context.sensitive_values,
            )
            print(f"已恢复会话：{session.session_id}", file=output)
            if restored.needs_time_span_reminder:
                print("该会话距离上次更新已超过 24 小时，请先确认当前状态。", file=output)
            continue
        if text == PLAN_COMMAND:
            mode = AgentMode.PLAN
            print("已切换到 Plan Mode。", file=output)
            continue
        if text == DO_COMMAND:
            mode = AgentMode.DO
            print("已切换到 Do Mode。", file=output)
            continue
        if text == COMPACT_COMMAND:
            status = context_manager.manual_compact(
                lambda prompt: _generate_summary(provider, prompt)
            )
            messages = {
                "compacted": "上下文已压缩。",
                "no_history": "没有可压缩的历史。",
                "failed": "上下文压缩未完成。",
            }
            print(messages[status], file=output)
            continue

        print("NewCode> ", end="", file=output, flush=True)
        loop = AgentLoop(
            provider=provider,
            session=session,
            registry=registry,
            tool_context=tool_context,
            permission_manager=permission_manager,
            context_manager=context_manager,
            memory_service=memory_service,
        )

        try:
            natural = _consume_agent_events(
                loop.run(text, mode=mode),
                output=output,
                error_output=error_output,
            )
            if natural:
                _safe_checkpoint(session_archive, session)
        except KeyboardInterrupt:
            print("\n已中断当前任务。", file=output)
        print("", file=output)
    finally:
        _safe_checkpoint(session_archive, session)
        _safe_cleanup_stale(session_archive, session.session_id)
        _safe_memory_shutdown(memory_service)
        _safe_context_cleanup(context_manager)


def _generate_summary(provider: ChatProvider, prompt: str) -> str:
    from newcode.session import ChatMessage

    parts: list[str] = []
    for event in provider.stream_chat(
        [
            ChatMessage(role="system", content="你是上下文摘要器。"),
            ChatMessage(role="user", content=prompt),
        ],
        tools=[],
        allow_tool_calls=False,
    ):
        if hasattr(event, "text"):
            parts.append(event.text)
    return "".join(parts)


def _generate_memory(provider: ChatProvider, request: MemoryGenerationRequest) -> str:
    """MemoryService 的唯一生成桥接：不注册工具，也不读取文件。"""

    parts: list[str] = []
    for event in provider.stream_chat(
        [
            ChatMessage(role="system", content="你是受控的本地记忆去重器。"),
            ChatMessage(role="user", content=request.prompt),
        ],
        tools=list(request.tools),
        allow_tool_calls=request.allow_tool_calls,
    ):
        if hasattr(event, "text"):
            parts.append(event.text)
    return "".join(parts)


def _consume_agent_events(
    events,
    *,
    output: TextIO,
    error_output: TextIO,
) -> bool:
    printed_text = False
    natural = False
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
            natural = True
            if event.content and not printed_text:
                print(event.content, end="", file=output, flush=True)
            continue

        if isinstance(event, AgentUsage):
            continue
    return natural


def _safe_create_session(archive: SessionArchive, session: ChatSession) -> None:
    try:
        archive.create(session)
    except Exception:
        return


def _safe_checkpoint(archive: SessionArchive | None, session: ChatSession) -> None:
    if archive is None or session.session_id is None:
        return
    try:
        archive.checkpoint(session)
    except Exception:
        return


def _safe_cleanup_stale(archive: SessionArchive | None, active_session_id: str | None) -> None:
    if archive is None:
        return
    try:
        archive.cleanup_stale(active_session_id=active_session_id)
    except Exception:
        return


def _safe_memory_shutdown(memory_service: MemoryService | None) -> None:
    if memory_service is None:
        return
    try:
        memory_service.shutdown()
    except Exception:
        return


def _safe_context_cleanup(context_manager: ContextManager) -> None:
    try:
        context_manager.cleanup()
    except Exception:
        return


def _print_sessions(archive: SessionArchive | None, output: TextIO) -> None:
    if archive is None:
        print("没有可恢复的会话。", file=output)
        return
    try:
        summaries = archive.list_recoverable()
    except Exception:
        summaries = ()
    if not summaries:
        print("没有可恢复的会话。", file=output)
        return
    for summary in summaries:
        print(
            f"{summary.session_id} | {summary.title} | {summary.updated_at.isoformat()} | {summary.message_count} 条消息",
            file=output,
        )


def _resume_session(archive: SessionArchive | None, text: str):
    if archive is None:
        return None
    parts = text.split()
    if len(parts) != 2 or parts[0] != RESUME_COMMAND:
        return None
    try:
        restored = archive.restore(parts[1])
    except Exception:
        return None
    return restored if restored.session is not None else None


def _build_permission_manager(
    *,
    mode: PermissionMode,
    rules: PermissionRulesLoadResult | None,
    confirmer: PermissionConfirmer,
) -> PermissionManager:
    if rules is None:
        return PermissionManager(mode=mode, confirmer=confirmer)
    return PermissionManager(
        mode=mode,
        local_project_rules=rules.local_project_rules,
        project_rules=rules.project_rules,
        user_global_rules=rules.user_global_rules,
        rule_load_errors=rules.errors,
        confirmer=confirmer,
    )
