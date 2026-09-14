from newcode.context.summary import parse_summary_response


def _valid():
    sections = "".join(f"## {name}\n证据：x；推断：无；未确认：无\n" for name in ("用户目标与原文约束", "当前任务状态", "关键事实与决策", "文件与工具结果索引", "验证证据", "后续动作与未决问题", "安全与边界"))
    return f"<analysis_draft>draft</analysis_draft><structured_summary>{sections}</structured_summary>"


def test_parser_keeps_only_structured_summary():
    value = _valid()
    assert parse_summary_response(value) is not None
    assert "draft" not in (parse_summary_response(value) or "")


def test_invalid_envelopes_fail():
    assert parse_summary_response(_valid() + "x") is None
    assert parse_summary_response("<structured_summary>x</structured_summary>") is None
    assert parse_summary_response(_valid().replace("</structured_summary>", "</structured_summary><structured_summary>x</structured_summary>")) is None
    assert parse_summary_response("<structured_summary>x</structured_summary><analysis_draft>x</analysis_draft>") is None
