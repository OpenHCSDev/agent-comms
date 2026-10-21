"""Capacity reports the existing load formula and working forks without policy advice."""

from dataclasses import replace

import pytest

from agent_comms.commander.capacity import Load
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_root import LaunchRoot
from agent_comms.thread_status import IdleThreadStatus, RunningThreadStatus, StoppedThreadStatus
from agent_comms.thread_text import ForkedParent

pytest_plugins = ("test_holder_standing_rules",)


@pytest.mark.parametrize("unjudged", [0, 1, 395, 1_000_000])
def test_capacity_reports_load_and_forks(unjudged):
    load = Load(24, 14, unjudged, ("judge",))
    assert FieldCodec.encode(load)["total"] == load.total
    assert load.total == 24 + 2 * 14 + unjudged / 10
    assert load.share == load.total / 2
    assert FieldCodec.encode(load)["share"] == load.share
    assert replace(load, forks=("judge", "reviewer")).share < load.share


@pytest.mark.parametrize("presence,expected", (
    (RunningThreadStatus(), ("worker",)),
    (IdleThreadStatus(), ("worker",)),
    (StoppedThreadStatus(), ()),
))
def test_claimed_child_capacity_uses_receiving_presence(held_bus, claim_order, presence, expected):
    comms = held_bus
    for problem in ("P1", "P2"):
        claim_order(problem)
    child = replace(comms.registry.require("worker"), parent=ForkedParent("lead"))
    comms.registry.register(child, presence)
    assert not any(todo.pending for todo in comms.goals.todos.held_by(
        comms.registry.require("lead").incarnation, child.incarnation))
    with LaunchRoot.from_path(comms.root).selected():
        assert Load.of(comms, "lead").forks == expected


def test_capacity_does_not_mix_new_children_into_a_captured_frame(held_bus):
    from agent_comms.ui_model.unit_state import UnitState

    states = UnitState.current(held_bus, "lead")
    child = held_bus.registry.new_thread(
        "late-fork", frozenset(), str(held_bus.root), parent=ForkedParent("lead"))
    held_bus.registry.register(child, IdleThreadStatus())
    assert "late-fork" not in Load.from_states(held_bus, "lead", states).forks
    assert "late-fork" in Load.of(held_bus, "lead").forks


