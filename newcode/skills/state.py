"""不持久化的会话级 Skill activation 状态。"""

from __future__ import annotations

from .types import LoadedSkill, SkillActivation, SkillCatalog


class ActiveSkillState:
    """保存加载时快照；不读取文件、不执行工具、不接触 ChatSession。"""

    def __init__(self) -> None:
        self._activations: tuple[SkillActivation, ...] = ()
        self._next_sequence = 1

    @property
    def activations(self) -> tuple[SkillActivation, ...]:
        return self._activations

    def activate(self, loaded: LoadedSkill) -> SkillActivation:
        activation = SkillActivation(loaded=loaded, sequence=self._next_sequence)
        self._next_sequence += 1
        self._activations = tuple(
            item for item in self._activations if item.loaded.metadata.frontmatter.name != loaded.metadata.frontmatter.name
        ) + (activation,)
        return activation

    def refresh(self, catalog: SkillCatalog) -> None:
        current = {item.frontmatter.name: item for item in catalog.skills}
        refreshed: list[SkillActivation] = []
        for activation in self._activations:
            latest = current.get(activation.loaded.metadata.frontmatter.name)
            if latest is None:
                continue
            changed = (
                latest.digest != activation.loaded.metadata.digest
                or latest.entry != activation.loaded.metadata.entry
                or latest.source != activation.loaded.metadata.source
            )
            refreshed.append(
                SkillActivation(activation.loaded, activation.sequence, activation.stale or changed)
            )
        self._activations = tuple(refreshed)

    def clear(self) -> None:
        self._activations = ()

    def reset_for_new_session(self) -> None:
        self.clear()

    def reset_for_resume(self) -> None:
        self.clear()

    def prompt_background(self) -> str:
        blocks: list[str] = []
        for activation in self._activations:
            frontmatter = activation.loaded.metadata.frontmatter
            parameters = ", ".join(name for name, _ in activation.loaded.parameters) or "无"
            blocks.append(
                "【受控 Skill 指令｜不授予权限】\n"
                f"名称：{frontmatter.name}\n"
                f"来源：{activation.loaded.metadata.source.value}\n"
                f"模式：{frontmatter.mode.value}\n"
                f"工具白名单：{', '.join(frontmatter.tools)}\n"
                f"参数：{parameters}\n"
                f"版本：{activation.loaded.metadata.digest}\n"
                "SOP：\n"
                f"{activation.loaded.sop}"
            )
        return "\n\n".join(blocks)
