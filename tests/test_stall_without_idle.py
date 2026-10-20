"""Real copied-registry branches plus the objective alarm owners."""
import time
from dataclasses import replace

import pytest

from agent_comms.child_process import CountPrinted
from agent_comms.commander import standing
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsHistoryTool
from agent_comms.todos import (
    DistanceReading, DistanceReadings, MergedProgress, OpenPrProgress, PushedProgress,
    ReviewReadyEvent, ReviewReadyEvents, UnmergedListedPullRequest,
)
from prove_stall_without_idle import prove
from test_commander_problem_orders import create


@pytest.mark.parametrize("spare", [False, True])
def test_real_flat_rule_on_a_copied_registry(tmp_path, spare):
    receipt = prove(tmp_path / "journey", spare)
    assert receipt.original_unchanged
    assert receipt.strikes == 1
    assert receipt.holder == ("spare" if spare else "worker")


def test_ready_clock_comes_from_review_event_not_recent_updates():
    row = UnmergedListedPullRequest(
        1, "fix/order", "a" * 40, updated="2026-10-19T22:00:00Z", body="", base="main",
        draft=False, created="2026-10-19T20:00:00Z",
        review=ReviewReadyEvents((ReviewReadyEvent("2026-10-19T21:00:00Z"),)))
    ready = row.parsed().ready.recorded_at("Review readiness")
    assert ready < row.parsed().updated_at
    assert not replace(row, draft=True).parsed().ready.before(float("inf"))
    assert OpenPrProgress(1, "a" * 9, at=ready + 299, ready=RecordedEventTime(ready)).review_late(
        ready + 301, 300) == (ready + 300,)
    assert OpenPrProgress(1, "a" * 9, at=ready).review_late(ready + 1000, 300) == ()


def test_objective_alarms_are_declarations_and_count_against_commander(commander, monkeypatch):
    comms, _ = commander
    todo = create(commander, "P1")
    now = time.time()
    observations = (
        (standing.MergedInstallLate, todo,
         MergedProgress("a" * 40, at=RecordedEventTime(now - 601))),
        (standing.ReadyReviewLate, todo, OpenPrProgress(
            1, "b" * 9, at=now, ready=RecordedEventTime(now - 301))),
        (standing.WorkTimeBeyondDue, replace(todo, deadline=RecordedEventTime(now - 5)),
         PushedProgress("c" * 9, at=now - 10, readings=DistanceReadings((
             DistanceReading(now - 20, CountPrinted(2)),
             DistanceReading(now - 10, CountPrinted(1)))))),
    )
    for rule_type, row, progress in observations:
        def observed(cls, opened, commander):
            return iter(((row, progress),))
        monkeypatch.setattr(standing.CommanderAlarm, "observed", classmethod(observed))
        rules = rule_type.violations(comms, "lead")
        assert len(rules) == 1
        rules[0].enforce(comms, "lead")
    page = CommsHistoryTool(peer="#comms").apply(comms, ThreadCaller("lead"))
    assert len(page.messages) == 3
    assert all("COMMANDER lead:" in message.body for message in page.messages)
    assert [standing.MergedInstallLate.seconds, standing.ReadyReviewLate.seconds,
            standing.WorkTimeBeyondDue.seconds] == [600, 300, 0]
