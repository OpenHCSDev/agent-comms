"""A held phase's clock reads the real stored order history, not an inline tuple double."""
from dataclasses import replace

import pytest

from agent_comms.nominal.field_codec import OrderId
from agent_comms.planning.nominal_census import NominalCensus
from agent_comms.planning.order_phase import UnplannedPhase
from agent_comms.planning.order_words import StrikeReason
from agent_comms.planning.orders import ReleaseTodo, StoredOrderStory
from agent_comms.planning.phase_evidence import MainTree
from agent_comms.planning.plan_phases import BusWriterPhase, PlanPhase
from agent_comms.planning.plan_reading import PlanReading
from agent_comms.storage.recorded_time import RecordedEventTime
from test_todo_stories import recorded_problem


def held_order(comms, number: int, at: float, previous: tuple[float, ...] = ()):
    store = comms.goals.todos
    original = store.get("P1")
    current = replace(original, id=f"P{number}", order=replace(original.order.checked(),
        id=OrderId.mint(), at=RecordedEventTime(at), serves=BusWriterPhase))
    with store.writing() as db:
        current.upsert(db)
        for timestamp in previous:
            StoredOrderStory.append(db, current.id, replace(current.order.checked(),
                id=OrderId.mint(), at=RecordedEventTime(timestamp)))
    return store.get(current.id)


def test_a_resent_order_keeps_the_first_orders_time(recorded_problem, source_repository):
    current = held_order(recorded_problem, 1, 5000, previous=(100,))
    reading = PlanReading((current,), MainTree.of(source_repository, NominalCensus()))
    assert BusWriterPhase.ordered_since(reading) == 100


def test_the_oldest_held_order_sets_the_clock(recorded_problem, source_repository):
    first = held_order(recorded_problem, 1, 300)
    second = held_order(recorded_problem, 2, 200)
    reading = PlanReading((first, second), MainTree.of(source_repository, NominalCensus()))
    assert BusWriterPhase.ordered_since(reading) == 200


def test_an_unheld_order_does_not_count(recorded_problem, source_repository):
    comms = recorded_problem
    current = held_order(comms, 1, 50)
    released = comms.goals.todos.apply(current.id, ReleaseTodo(current.revision,
        current.held().owner_identity, current.held(),
        StrikeReason("The holder releases this order so another worker can repair the owner.")))
    reading = PlanReading((released,), MainTree.of(source_repository, NominalCensus()))
    assert not BusWriterPhase.ordered(reading)


def unavailable_plan() -> tuple[type[PlanPhase], ...]:
    raise OSError("team plan missing")


def test_explicit_unplanned_reason_needs_no_plan_acquisition():
    reason = "This repair keeps the order tool usable before the planned work resumes."
    assert UnplannedPhase.order_refusals(unavailable_plan, reason) == ()


@pytest.mark.parametrize("reason", ("", "A short reason"))
def test_missing_unplanned_reason_still_requires_plan_evidence(reason):
    with pytest.raises(OSError, match="team plan missing"):
        UnplannedPhase.order_refusals(unavailable_plan, reason)


def test_ready_phases_refuse_an_order_with_no_phase_or_reason():
    assert UnplannedPhase.order_refusals(lambda: (BusWriterPhase,), "") == (
        f"Plan phases are ready: {BusWriterPhase.declared_name}. "
        "Name a phase, or provide an explicit unplanned_reason of 8+ words.",)
    assert UnplannedPhase.order_refusals(lambda: (), "") == ()


def test_a_named_plan_phase_needs_no_unrelated_plan_acquisition():
    assert BusWriterPhase.order_refusals(unavailable_plan, "") == ()
