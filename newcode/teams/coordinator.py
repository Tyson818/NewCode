"""Coordinator 封闭能力集合；不是 shell 或网络的授权来源。"""

from dataclasses import dataclass
from .tools import TEAM_TOOLS

COORDINATOR_TOOLS = frozenset({"read_file", "find_files", "search_code"}) | TEAM_TOOLS


@dataclass(frozen=True)
class CoordinatorPolicy:
    enabled: bool = False

    @classmethod
    def from_config(cls, config, environment):
        return cls(config.coordinator_active(environment))

    def filter(self, names):
        return frozenset(names) & COORDINATOR_TOOLS if self.enabled else frozenset(names)
