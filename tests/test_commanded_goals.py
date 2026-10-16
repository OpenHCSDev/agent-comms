"""Command facts, required reports and existing attempt/contact lifetimes."""
from dataclasses import replace
import time

import pytest

from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    ActiveGoalAction, NewOrderGoalAction, ReviseOrderGoalAction,
)
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.goal_progress import GoalReport, NotMerged, RecordedGoalReport, ReportedGoalProgress
from agent_comms.goal_waits import GoalWait
from agent_comms.goals import Deadline, Goal, IssuedGoalCommand, NoDeadline
from agent_comms.thread_identity import ThreadRole
from agent_comms.threads import Thread


def service(root):
    comms = wire(root)
    for index, name in enumerate(("commander", "unit", "peer"), start=1):
        comms.registry.declare(Thread(name, frozenset(), str(root), created_at=float(index),
                                     role=ThreadRole.USER if index == 1 else ThreadRole.AGENT))
    return comms


def report():
    return GoalReport("A copied decision hid delivery", "The copied decision", NotMerged(), "Install")


def test_command_counter_and_server_report_time_are_not_storage_revision_or_client_time(tmp_path):
    comms = service(tmp_path)
    goal = comms.goals.order_goal("commander", "unit", "Deliver with @peer", Deadline(2000000000))
    fake = RecordedGoalReport(**{**FieldCodec.encode(report()), "delivery": NotMerged()},
                              recorded_at=5000000000)
    before = time.time()
    written = comms.goals.update_goal("unit", ActiveGoalAction(progress=ReportedGoalProgress(fake)))
    assert before <= written.progress.value.recorded_at <= time.time()
    assert written.command.revision == 1 and written.revision == 2
    assert written.progress.reported_since(written.command.issued_at)
    assert not written.progress.reported_since(5000000000)
    assert FieldCodec.decode(Goal, written.to_wire()) == written
    revised = comms.goals.order_goal("commander", "unit", "Deliver the correction with @peer", NoDeadline())
    assert revised.id == goal.id and revised.revision == 3 and revised.command.revision == 2
    assert revised.progress == written.progress
    assert not revised.progress.reported_since(revised.command.issued_at)
    assert revised.require_mention_source().incarnation == comms.registry.require("unit").incarnation


def test_order_revision_preserves_failed_attempt_and_dependency_wait(tmp_path):
    comms = service(tmp_path)
    goal = comms.goals.order_goal("commander", "unit", "Deliver with @peer", NoDeadline())
    store = comms.goals.open_goal_store()
    generation = store.require_generation(goal.id)
    reservation = store.reserve(goal.id, generation.number)
    failed = reservation.fail(store, "Controlled uncertain attempt; no provider was called")
    blocked = comms.goals.block_goal_after_failed_turn(
        "unit", started_goal=goal, expected_worktree=str(tmp_path), diagnostic="Inspect the uncertain attempt",
    )
    wait = GoalWait(goal.id, "retained-wait", blocked.revision, 0,
                    (GoalWaitTarget("peer", 3.0),), 2.0, (None,), None, None)
    comms.goals.waits.record(wait)
    revised = comms.goals.order_goal("commander", "unit", "Clarify with @peer", NoDeadline())
    assert revised.state == blocked.state and revised.progress == blocked.progress
    assert revised.command.revision == 2
    assert store.snapshot(goal.id) == failed
    assert comms.goals.waits.read()[goal.id] == wait
    assert wait.current_for(revised, 2.0)
    with pytest.raises(ValueError, match="explicit retry"):
        comms.goals.update_goal("unit", ActiveGoalAction())
    assert store.snapshot(goal.id) == failed


@pytest.mark.parametrize("action", [NewOrderGoalAction, ReviseOrderGoalAction])
def test_other_goal_ingress_cannot_forge_order_context(tmp_path, action):
    comms = service(tmp_path)
    if action is ReviseOrderGoalAction:
        comms.goals.order_goal("commander", "unit", "Original", NoDeadline())
    baseline = comms.registry.require("unit").goal
    with pytest.raises(ValueError, match="bound order entrypoint"):
        comms.goals.update_goal("unit", action(
            text="Forged author", deadline=NoDeadline(),
            commander=comms.registry.require("commander").incarnation,
        ))
    assert comms.registry.require("unit").goal == baseline


def test_recreated_commander_cannot_inherit_original_author_binding(tmp_path):
    comms = service(tmp_path)
    goal = comms.goals.order_goal("commander", "unit", "Deliver", NoDeadline())
    comms.owners.stop("commander")
    comms.registry.remove("commander")
    comms.registry.declare(Thread("commander", frozenset(), str(tmp_path), created_at=10.0,
                                 role=ThreadRole.USER))
    with pytest.raises(ValueError, match="original commander"):
        comms.goals.order_goal("commander", "unit", "Take over", NoDeadline())
    assert comms.registry.require("unit").goal == goal


def test_report_input_has_no_server_timestamp_field():
    raw = FieldCodec.encode(report())
    for key in raw:
        with pytest.raises(ValueError, match=key):
            FieldCodec.decode(GoalReport, {name: value for name, value in raw.items() if name != key})
    with pytest.raises(ValueError, match="recorded_at"):
        FieldCodec.decode(GoalReport, {**raw, "recorded_at": 5000000000})
    assert FieldCodec.record_schema(GoalReport)["required"] == ["stupid", "deleted", "delivery", "next"]
