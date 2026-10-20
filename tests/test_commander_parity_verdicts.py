"""Observation invariants; the configured LifecycleRun journey supplies native proof."""

from dataclasses import replace

from agent_comms.commander_parity.rows import VerdictParityRow
from agent_comms.commander_parity.verdicts import (
    VerdictComparison,
    VerdictMutation,
    VerdictObservation,
)


def observation(mutation: VerdictMutation, changes: int) -> VerdictObservation:
    return VerdictObservation("native verdict owner", 1, mutation.checks(changes), "", mutation)


def test_verdict_requires_the_actual_call_event_goal_and_strike_together():
    accepted = VerdictMutation("one-call", 1, 1, 0, 0, 0, "P1: Open · Accepted")
    back = replace(
        accepted,
        strikes=1,
        goal_revisions=1,
        goal_history_entries=1,
        progress="P1: Open · Sent back",
    )
    ok, sent_back = observation(accepted, 0), observation(back, 1)
    compared = VerdictComparison(ok, ok, sent_back, sent_back)
    assert compared.passed
    for changed in (
        replace(back, verdicts=0),
        replace(back, todo_revisions=2),
        replace(back, strikes=0),
        replace(back, goal_revisions=0),
        replace(back, goal_history_entries=2),
    ):
        observed = observation(changed, 1)
        assert not observed.passed
        assert not replace(compared, back_current=observed, back_native=observed).passed
    for changed in (
        replace(sent_back, invocations=0),
        replace(sent_back, checks=(*sent_back.checks, ("retained native identity", False))),
    ):
        assert not replace(compared, back_current=changed, back_native=changed).passed
    for changed in (replace(back, call_id="other-call"), replace(back, progress="")):
        assert not replace(compared, back_native=observation(changed, 1)).passed
    assert "verify" not in VerdictParityRow.__dict__
    assert VerdictParityRow.comparison == VerdictComparison.read
