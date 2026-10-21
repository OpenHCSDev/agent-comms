"""A held plan phase's clock starts at its first order; re-sending never restarts it."""

from dataclasses import dataclass

import pytest

from agent_comms.commander import standing
from agent_comms.phase_evidence import PlanReading
from agent_comms.plan_phases import BusWriterPhase, PlanPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import OpenTodoState


@dataclass(frozen=True)
class SentOrder:
    """Stands in for a checked order: the phase it serves and when it was sent."""

    at: RecordedEventTime
    phase: type = BusWriterPhase

    def checked(self) -> "SentOrder":
        return self


@dataclass(frozen=True)
class HeldTodo:
    order: SentOrder
    prior_orders: tuple[SentOrder, ...] = ()
    state: type = OpenTodoState
    holder: str = "unit-bus-w"


def reading(*todos: HeldTodo) -> PlanReading:
    return PlanReading(todos, main=None)


def test_a_resent_order_keeps_the_first_orders_time():
    resent = HeldTodo(SentOrder(RecordedEventTime(5000.0)),
                      prior_orders=(SentOrder(RecordedEventTime(100.0)),))
    assert BusWriterPhase.ordered_since(reading(resent)) == 100.0


def test_the_oldest_holder_sets_the_clock():
    first = HeldTodo(SentOrder(RecordedEventTime(300.0)))
    second = HeldTodo(SentOrder(RecordedEventTime(200.0)), holder="unit-bus-b1")
    assert BusWriterPhase.ordered_since(reading(first, second)) == 200.0


def test_an_unheld_order_does_not_count():
    released = HeldTodo(SentOrder(RecordedEventTime(50.0)), holder="")
    assert not BusWriterPhase.ordered(reading(released))


FIRST = 1000.0


@pytest.fixture
def stalled_reading(monkeypatch):
    """BusWriterPhase is the only phase, ready unless a test lands its evidence."""
    held = {"reading": reading(HeldTodo(SentOrder(RecordedEventTime(FIRST))))}
    monkeypatch.setattr(standing.PlanRule, "plan_reading",
                        staticmethod(lambda comms, commander: held["reading"]))
    monkeypatch.setattr(PlanPhase, "members_with", classmethod(lambda cls, base: (BusWriterPhase,)))
    monkeypatch.setattr(BusWriterPhase, "ready", classmethod(lambda cls, reading: True))
    return held


def stalled_at(monkeypatch, now: float) -> tuple:
    monkeypatch.setattr(standing.time, "time", lambda: now)
    return standing.PlanStalled.violations(None, "claude-code")


def test_fires_past_the_window(stalled_reading, monkeypatch):
    (rule,) = stalled_at(monkeypatch, FIRST + standing.PlanStalled.window + 1)
    assert (rule.phase, rule.since) == (BusWriterPhase, FIRST)


def test_quiet_under_the_window(stalled_reading, monkeypatch):
    assert stalled_at(monkeypatch, FIRST + standing.PlanStalled.window - 1) == ()


def test_clears_when_the_phase_lands_on_main(stalled_reading, monkeypatch):
    monkeypatch.setattr(BusWriterPhase, "ready", classmethod(lambda cls, reading: False))
    assert stalled_at(monkeypatch, FIRST + 10 * standing.PlanStalled.window) == ()


def test_a_replacement_order_keeps_it_firing(stalled_reading, monkeypatch):
    replaced = FIRST + standing.PlanStalled.window
    stalled_reading["reading"] = reading(HeldTodo(
        SentOrder(RecordedEventTime(replaced)), prior_orders=(SentOrder(RecordedEventTime(FIRST)),),
    ))
    assert stalled_at(monkeypatch, replaced + 60)
