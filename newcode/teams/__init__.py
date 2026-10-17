"""Team 的纯数据与本地持久化边界；导入时不创建运行对象。"""

from .types import TeamError, TeamManifest, TeamMemberRecord, TeamMessage, TeamPlan, TeamTask

__all__ = ["TeamError", "TeamManifest", "TeamMemberRecord", "TeamMessage", "TeamPlan", "TeamTask"]
