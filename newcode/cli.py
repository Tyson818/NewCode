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
from newcode.commands.builtins import CommandRuntime, create_builtin_registry
from newcode.commands.dispatcher import CommandDispatcher
from newcode.commands.types import CommandOutcomeKind, CommandParseKind
from newcode.commands.ui import CLIUIControl
from newcode.context.manager import ContextManager
from newcode.hooks.actions import (
    BoundedHostResolver, HookActionRunner, HookToolGateway, PinnedHttpsTransport,
)
from newcode.hooks.engine import HookEngine
from newcode.hooks.loader import load_hook_rules
from newcode.hooks.types import HookContext, HookEvent
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
from newcode.skills.commands import SkillCommandDispatcher, SkillCommandOverlay
from newcode.skills.discovery import SkillDiscovery
from newcode.skills.policy import visible_tool_names
from newcode.skills.runner import SkillRunner
from newcode.skills.state import ActiveSkillState


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
    memory_cleanup_state = {"attempted": False}
    mcp_status_summary = "MCP 状态未提供。"
    try:
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
            discovered_servers = mcp_manager.discover_all()
            for name, descriptors in discovered_servers.items():
                config_entry = mcp_config.servers[name]
                for descriptor in descriptors:
                    try:
                        validate_input_schema(descriptor.input_schema)
                        adapter = MCPToolAdapter(mcp_manager, config_entry, descriptor)
                        registry.register(adapter, read_only=False, do_visible=True)
                    except (MCPToolSchemaError, ValueError):
                        print(f"MCP tool unavailable ({name}): mcp_tool_schema_invalid", file=sys.stderr)
            discovered_tools = sum(len(descriptors) for descriptors in discovered_servers.values())
            mcp_status_summary = f"MCP 已配置 {len(mcp_config.servers)} 个服务，已发现 {discovered_tools} 个工具。"
        except ConfigError as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            return 1
        except Exception:
            print("启动失败：startup_failed", file=sys.stderr)
            return 1

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
            mcp_status_summary=mcp_status_summary,
            memory_cleanup_state=memory_cleanup_state,
        )
    finally:
        if memory_service is not None and not memory_cleanup_state["attempted"]:
            _safe_memory_shutdown(memory_service)
        if mcp_manager is not None:
            try:
                mcp_manager.shutdown()
            except Exception:
                pass
        if mcp_runtime is not None:
            try:
                mcp_runtime.shutdown()
            except Exception:
                pass


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
    mcp_status_summary: str = "MCP 状态未提供。",
    memory_cleanup_state: dict[str, bool] | None = None,
) -> int:
    hook_actions: HookActionRunner | None = None
    hook_engine: HookEngine | None = None
    try:
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
        loop: AgentLoop | None = None
        command_registry = create_builtin_registry()
        skill_state = ActiveSkillState()
        skill_discovery = SkillDiscovery()
        skill_catalog = skill_discovery.discover(tool_context.workspace_root)
        command_dispatcher = SkillCommandDispatcher(
            CommandDispatcher(command_registry),
            SkillCommandOverlay(command_registry, skill_state),
        )
        command_ui = CLIUIControl(output=output, error_output=error_output)
        try:
            hook_config = load_hook_rules(tool_context.workspace_root)
            for diagnostic in hook_config.diagnostics:
                print(f"Hook 配置跳过：{diagnostic.code}", file=error_output)
            if hook_config.rules:
                http_enabled = hook_config.network.enabled and bool(hook_config.network.allow_hosts)
                hook_actions = HookActionRunner(
                    network=hook_config.network,
                    resolver=BoundedHostResolver() if http_enabled else None,
                    http_transport=PinnedHttpsTransport() if http_enabled else None,
                    shell_gateway=HookToolGateway(
                        permission_manager, registry, tool_context,
                        tool_visible=lambda name: name in visible_tool_names(mode, registry, skill_state),
                    ),
                    sensitive_values=tool_context.sensitive_values,
                )
                hook_engine = HookEngine(
                    hook_config.rules,
                    action_sink=hook_actions.submit,
                    on_session_reset=hook_actions.reset_session,
                )
        except Exception:
            _safe_hook_shutdown(hook_actions)
            hook_actions = None
            hook_engine = None
            print("Hook 配置跳过：hook_config_invalid", file=error_output)
    except BaseException:
        _safe_hook_shutdown(hook_actions)
        _safe_memory_shutdown(memory_service)
        if memory_cleanup_state is not None:
            memory_cleanup_state["attempted"] = True
        if context_manager is not None:
            _safe_context_cleanup(context_manager)
        raise

    system_open = False
    session_open = False
    try:
      print("NewCode 已启动。输入问题开始对话，输入 /exit 退出。", file=output)
      _safe_emit_cli_hook(hook_engine, HookEvent.SYSTEM_START, session, mode, tool_context)
      system_open = True
      _safe_emit_cli_hook(hook_engine, HookEvent.SESSION_START, session, mode, tool_context)
      session_open = True
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
        parsed = command_dispatcher.dispatch_input(user_input, command_ui)
        if parsed.kind is CommandParseKind.EMPTY:
            continue
        if parsed.kind is CommandParseKind.UNKNOWN:
            continue
        if parsed.kind is CommandParseKind.COMMAND:
            if parsed.command is None:
                continue
            outcome = command_dispatcher.execute(
                parsed.command,
                _command_runtime(
                    command_registry,
                    context_manager,
                    session,
                    mode,
                    session_archive,
                    memory_service,
                    permission_manager,
                    mcp_status_summary,
                    provider,
                ),
                command_ui,
            )
            if outcome.kind is CommandOutcomeKind.HANDLED:
                continue
            if outcome.kind is CommandOutcomeKind.MODE_CHANGE:
                mode = AgentMode(outcome.mode or AgentMode.DO.value)
                command_ui.set_mode(mode.value)
                continue
            if outcome.kind is CommandOutcomeKind.CLEAR_SESSION:
                _safe_emit_cli_hook(hook_engine, HookEvent.SESSION_END, session, mode, tool_context)
                session_open = False
                session, context_manager = _clear_session(
                    session,
                    context_manager,
                    session_archive,
                    tool_context,
                )
                skill_state.reset_for_new_session()
                loop = None
                if hook_engine is not None:
                    hook_engine.reset_session()
                _safe_emit_cli_hook(hook_engine, HookEvent.SESSION_START, session, mode, tool_context)
                session_open = True
                skill_catalog = skill_discovery.discover(tool_context.workspace_root)
                command_ui.info("已开始新会话。")
                continue
            if outcome.kind is CommandOutcomeKind.RESUME_SESSION:
                restored = _resume_session(session_archive, f"/resume {outcome.session_id or ''}")
                if restored is None:
                    command_ui.error("session_restore_failed", "会话不可恢复。")
                    continue
                _safe_emit_cli_hook(hook_engine, HookEvent.SESSION_END, session, mode, tool_context)
                session_open = False
                _safe_checkpoint(session_archive, session)
                _safe_context_cleanup(context_manager)
                session = restored.session
                context_manager = ContextManager(
                    session,
                    tool_context.workspace_root,
                    tool_context.sensitive_values,
                )
                skill_state.reset_for_resume()
                loop = None
                if hook_engine is not None:
                    hook_engine.reset_session()
                _safe_emit_cli_hook(hook_engine, HookEvent.SESSION_START, session, mode, tool_context)
                session_open = True
                skill_catalog = skill_discovery.discover(tool_context.workspace_root)
                command_ui.info(f"已恢复会话：{session.session_id}")
                if restored.needs_time_span_reminder:
                    command_ui.info("该会话距离上次更新已超过 24 小时，请先确认当前状态。")
                continue
            if outcome.kind is CommandOutcomeKind.AI_INPUT:
                text = outcome.ai_input or ""
            elif outcome.kind is CommandOutcomeKind.SKILL_REQUEST:
                activation = next(
                    (item for item in skill_state.activations if item.loaded.metadata.frontmatter.name == outcome.skill_name),
                    None,
                )
                if activation is None:
                    command_ui.error("skill_not_found", "Skill 当前不可用。")
                    continue
                runner = SkillRunner(
                    provider=provider,
                    registry=registry,
                    tool_context=tool_context,
                    permission_manager=permission_manager,
                    parent_session=session,
                    parent_context=context_manager,
                    skill_catalog=skill_catalog,
                )
                if activation.loaded.metadata.frontmatter.mode.value == "isolated":
                    result = runner.run_isolated(activation, outcome.skill_parameters, mode=mode)
                    if result.ok:
                        command_ui.info(result.summary)
                        _safe_checkpoint(session_archive, session)
                    else:
                        command_ui.error(result.code or "skill_isolated_failed", "Skill 未完成。")
                    continue
                text = runner.shared_input(activation, outcome.skill_parameters)
            else:
                continue

        print("NewCode> ", end="", file=output, flush=True)
        if loop is None:
            loop = AgentLoop(
                provider=provider,
                session=session,
                registry=registry,
                tool_context=tool_context,
                permission_manager=permission_manager,
                context_manager=context_manager,
                memory_service=memory_service,
                skill_state=skill_state,
                skill_discovery=skill_discovery,
                hook_engine=hook_engine,
                hook_actions=hook_actions,
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
        try:
            skill_state.clear()
        except Exception:
            pass
        if session_open:
            _safe_emit_cli_hook(hook_engine, HookEvent.SESSION_END, session, mode, tool_context)
        _safe_checkpoint(session_archive, session)
        _safe_cleanup_stale(session_archive, session.session_id)
        if system_open:
            _safe_emit_cli_hook(hook_engine, HookEvent.SYSTEM_END, session, mode, tool_context)
        _safe_hook_shutdown(hook_actions)
        _safe_memory_shutdown(memory_service)
        if memory_cleanup_state is not None:
            memory_cleanup_state["attempted"] = True
        _safe_context_cleanup(context_manager)


def _command_runtime(
    registry,
    context_manager: ContextManager,
    session: ChatSession,
    mode: AgentMode,
    archive: SessionArchive | None,
    memory_service: MemoryService | None,
    permission_manager: PermissionManager,
    mcp_status_summary: str,
    provider: ChatProvider,
) -> CommandRuntime:
    """仅把既有的受控本地查询与生命周期能力交给内置命令。"""

    def compact() -> str:
        return context_manager.manual_compact(lambda prompt: _generate_summary(provider, prompt))

    def list_sessions() -> tuple[object, ...]:
        if archive is None:
            return ()
        try:
            return archive.list_recoverable()
        except Exception:
            return ()

    def list_memory(scope: str) -> tuple[object, ...]:
        store = getattr(memory_service, "store", None)
        if store is None:
            return ()
        try:
            notes = store.select_for_prompt()
        except Exception:
            return ()
        return tuple(note for note in notes if scope == "all" or note.scope.value == scope)

    def permission_summary() -> str:
        try:
            session_rule_count = len(permission_manager.session_rules.rules)
            diagnostics = len(permission_manager.rule_load_errors)
        except Exception:
            return "权限状态暂不可用。"
        return (
            f"权限模式：{permission_manager.mode.value}；"
            f"会话规则：{session_rule_count} 条；安全诊断：{diagnostics} 项。"
        )

    def status_summary() -> str:
        try:
            estimated = context_manager.estimator.estimate(session.messages, session.context_version)
            circuit = "已熔断" if context_manager.circuit_open else "正常"
        except Exception:
            estimated = 0
            circuit = "不可用"
        memory = "已启用" if memory_service is not None else "未启用"
        return (
            f"模式：{mode.value}；会话：{session.session_id or '未归档'}；"
            f"上下文近似：{estimated} tokens；压缩熔断：{circuit}；"
            f"自动记忆：{memory}；{mcp_status_summary}"
        )

    return CommandRuntime(
        registry=registry,
        manual_compact=compact,
        list_sessions=list_sessions,
        list_memory=list_memory,
        permission_summary=permission_summary,
        status_summary=status_summary,
    )


def _clear_session(
    session: ChatSession,
    context_manager: ContextManager,
    archive: SessionArchive | None,
    tool_context: ToolContext,
) -> tuple[ChatSession, ContextManager]:
    """保存旧会话并清理其 artifact 后创建独立的新受控会话。"""

    _safe_checkpoint(archive, session)
    _safe_context_cleanup(context_manager)
    replacement = ChatSession()
    if archive is not None:
        _safe_create_session(archive, replacement)
    return (
        replacement,
        ContextManager(
            replacement,
            tool_context.workspace_root,
            tool_context.sensitive_values,
        ),
    )


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


def _safe_emit_cli_hook(
    engine: HookEngine | None,
    event: HookEvent,
    session: ChatSession,
    mode: AgentMode,
    tool_context: ToolContext,
) -> None:
    if engine is None:
        return
    try:
        engine.emit(
            event,
            HookContext(
                event,
                {
                    "session.id": session.session_id or f"in-memory-{id(session)}",
                    "mode": mode.value,
                },
                sensitive_values=tool_context.sensitive_values,
            ),
        )
    except Exception:
        return


def _safe_hook_shutdown(actions: HookActionRunner | None) -> None:
    if actions is None:
        return
    try:
        actions.shutdown(timeout_seconds=1.0)
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
