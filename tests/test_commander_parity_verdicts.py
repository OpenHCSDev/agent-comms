"""Comparison invariants; the configured LifecycleRun journey supplies native proof."""

from dataclasses import replace

from agent_comms.commander_parity.rows import VerdictParityRow
from agent_comms.commander_parity.verdicts import (
    AcceptedCase,
    SendBackCase,
    VerdictComparison,
    VerdictEffect,
)


def test_verdict_family_requires_the_actual_call_event_goal_and_strike_together():
    accepted = VerdictEffect(
        call_id="one-call",
        verdicts=1,
        todo_revisions=1,
        strikes=0,
        goal_revisions=0,
        goal_transitions=0,
        goal_retained=True,
        goal_state_retained=True,
        progress="P1: Open",
        native_calls=1,
        native_result_retained=True,
        strike_matches_verdict_event=True,
        declared_verdict_recorded=True,
        goal_transition_retained=True,
    )
    back = replace(
        accepted, strikes=1, goal_revisions=1, goal_transitions=1, progress=SendBackCase.why
    )
    compared = VerdictComparison(accepted, accepted, back, back)
    assert compared.passed and AcceptedCase.valid(accepted) and SendBackCase.valid(back)
    for changed in (
        replace(back, verdicts=0),
        replace(back, todo_revisions=2),
        replace(back, strikes=0),
        replace(back, goal_revisions=0),
        replace(back, goal_transitions=2),
        replace(back, native_calls=0),
        replace(back, goal_retained=False),
        replace(back, goal_state_retained=False),
        replace(back, native_result_retained=False),
        replace(back, strike_matches_verdict_event=False),
        replace(back, declared_verdict_recorded=False),
        replace(back, goal_transition_retained=False),
        replace(back, progress=""),
    ):
        assert not SendBackCase.valid(changed)
        assert not replace(compared, back_current=changed, back_native=changed).passed
    assert not replace(compared, accepted_native=replace(accepted, call_id="other-call")).passed
    assert "verify" not in VerdictParityRow.__dict__
    assert VerdictParityRow.comparison == VerdictComparison.read
