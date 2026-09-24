"""SubAgent 公共 API；runner 延迟导入以避免 AgentLoop 导入环。"""

from .discovery import AgentDiscovery
from .manager import SubAgentManager, WaitOutcome
from .types import (
    AgentCatalog,
    AgentDefinition,
    AgentDiagnostic,
    AgentDirectoryEntry,
    AgentPermissionMode,
    AgentSource,
    AgentValidationError,
    ParentPolicySnapshot,
    SessionScope,
    SubAgentManagerError,
    TaskBudget,
    TaskExecution,
    TaskResult,
    TaskSnapshot,
    TaskState,
    WorkerResult,
    WorkerTaskContext,
    parse_agent_frontmatter,
)

_RUNNER_EXPORTS = {
    "ChildReadCache", "DefinitionTask", "ForkTask", "ProviderFactory",
    "SubAgentRunner", "capture_fork_snapshot",
}
_POLICY_EXPORTS = {"child_tool_names", "definition_tool_names", "isolated_registry_view"}


def __getattr__(name: str):
    if name in _RUNNER_EXPORTS:
        from . import runner

        return getattr(runner, name)
    if name in _POLICY_EXPORTS:
        from . import policy

        return getattr(policy, name)
    raise AttributeError(name)


__all__ = [
    "AgentCatalog", "AgentDefinition", "AgentDiagnostic", "AgentDirectoryEntry",
    "AgentDiscovery", "AgentPermissionMode", "AgentSource", "AgentValidationError",
    "ParentPolicySnapshot", "SessionScope", "SubAgentManager", "SubAgentManagerError",
    "TaskBudget", "TaskExecution", "TaskResult", "TaskSnapshot", "TaskState",
    "WaitOutcome", "WorkerResult", "WorkerTaskContext", "parse_agent_frontmatter",
    *_RUNNER_EXPORTS, *_POLICY_EXPORTS,
]
