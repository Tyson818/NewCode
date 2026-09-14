from __future__ import annotations

import re

_ENVELOPE = re.compile(r"\A<analysis_draft>(.*?)</analysis_draft><structured_summary>(.*?)</structured_summary>\Z", re.DOTALL)
_SECTIONS = (
    "用户目标与原文约束",
    "当前任务状态",
    "关键事实与决策",
    "文件与工具结果索引",
    "验证证据",
    "后续动作与未决问题",
    "安全与边界",
)


def build_summary_prompt(history: str) -> str:
    return """仅总结所给历史。不得调用工具、读取文件或执行命令。\n严格输出且仅输出：<analysis_draft>草稿</analysis_draft><structured_summary>正式摘要</structured_summary>。\n正式摘要必须包含七个 Markdown 二级标题：用户目标与原文约束、当前任务状态、关键事实与决策、文件与工具结果索引、验证证据、后续动作与未决问题、安全与边界；并包含证据、推断、未确认标识。\n历史：\n""" + history


def parse_summary_response(response: str) -> str | None:
    match = _ENVELOPE.fullmatch(response)
    if match is None:
        return None
    summary = match.group(2)
    if any(tag in match.group(1) or tag in summary for tag in ("<analysis_draft>", "</analysis_draft>", "<structured_summary>", "</structured_summary>")):
        return None
    if not summary.strip() or any(f"## {section}" not in summary for section in _SECTIONS):
        return None
    if any(marker not in summary for marker in ("证据", "推断", "未确认")):
        return None
    return summary
