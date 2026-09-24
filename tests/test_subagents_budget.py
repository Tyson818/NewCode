from __future__ import annotations

from newcode.subagents.types import TaskBudget


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_task_budget_enforces_eight_rounds_and_tracks_approximate_usage():
    budget = TaskBudget(max_rounds=8, max_seconds=300, max_tokens=16_000)

    for _ in range(8):
        assert budget.begin_round("a short request")
        assert budget.record_output("a short response")
    assert budget.rounds == 8
    assert budget.input_tokens > 0
    assert budget.output_tokens > 0
    assert not budget.begin_round("ninth request")
    assert budget.stop_code == "subagent_iteration_limit"


def test_task_budget_uses_utf8_estimate_and_trusted_usage_upper_bound():
    budget = TaskBudget(max_rounds=8, max_seconds=300, max_tokens=20)

    assert budget.begin_round("你好", usage_tokens=9)
    assert budget.input_tokens == 9
    assert not budget.record_output("x", usage_tokens=12)
    assert budget.output_tokens == 0
    assert budget.stop_code == "subagent_token_budget_exceeded"


def test_task_budget_deadline_uses_injected_fake_clock():
    clock = FakeClock()
    budget = TaskBudget(clock=clock, max_seconds=300)

    assert budget.begin_round("request")
    clock.advance(300)
    assert not budget.begin_round("after deadline")
    assert budget.stop_code == "subagent_timeout"
    assert budget.remaining_seconds() == 0
