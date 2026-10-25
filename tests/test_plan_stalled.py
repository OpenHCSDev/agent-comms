"""A held phase's clock reads the real stored order history, not an inline tuple double."""
from dataclasses import replace

from agent_comms.phase_evidence import MainTree, PlanReading
from agent_comms.plan_phases import BusWriterPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import ReleaseTodo, StoredOrderStory, StrikeReason
from test_todo_stories import recorded_problem


def held_order(comms, number: int, at: float, previous: tuple[float, ...] = ()):
    store = comms.goals.todos
    original = store.get("P1")
    current = replace(original, id=f"P{number}", order=replace(original.order.checked(),
        id=f"order-{number}", at=RecordedEventTime(at), serves=BusWriterPhase))
    with store.writing() as db:
        current.upsert(db)
        for timestamp in previous:
            StoredOrderStory.append(db, current.id, replace(current.order.checked(),
                id=f"earlier-{number}-{timestamp}", at=RecordedEventTime(timestamp)))
    return store.get(current.id)


def test_a_resent_order_keeps_the_first_orders_time(recorded_problem, source_repository):
    current = held_order(recorded_problem, 1, 5000, previous=(100,))
    reading = PlanReading((current,), MainTree.of(source_repository))
    assert BusWriterPhase.ordered_since(reading) == 100


def test_the_oldest_held_order_sets_the_clock(recorded_problem, source_repository):
    first = held_order(recorded_problem, 1, 300)
    second = held_order(recorded_problem, 2, 200)
    reading = PlanReading((first, second), MainTree.of(source_repository))
    assert BusWriterPhase.ordered_since(reading) == 200


def test_an_unheld_order_does_not_count(recorded_problem, source_repository):
    comms = recorded_problem
    current = held_order(comms, 1, 50)
    released = comms.goals.todos.apply(current.id, ReleaseTodo(current.revision,
        comms.registry.require("lead").incarnation, current.held(),
        StrikeReason("The commander releases this order so another worker can repair the owner.")))
    reading = PlanReading((released,), MainTree.of(source_repository))
    assert not BusWriterPhase.ordered(reading)
