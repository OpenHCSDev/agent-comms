"""Capacity reports the existing load formula and captured working forks."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
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


@pytest.fixture
def working_turn(held_bus):
    leases = []

    def start(name):
        owner = held_bus.agents.begin_turn(name, f"capacity-{name}")
        leases.append(owner.thread.require_turn_lease())

    yield start
    for lease in reversed(leases):
        held_bus.agents.finish_turn(lease)


@pytest.mark.parametrize("presence,working,expected", (
    (RunningThreadStatus(), True, ("worker",)),
    (RunningThreadStatus(), False, ()),
    (IdleThreadStatus(), False, ()),
    (StoppedThreadStatus(), False, ()),
))
def test_claimed_child_capacity_uses_working_activity(
        held_bus, claim_order, working_turn, presence, working, expected):
    comms = held_bus
    for problem in ("P1", "P2"):
        claim_order(problem)
    child = replace(comms.registry.require("worker"), parent=ForkedParent("lead"),
                    process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.register(child, presence)
    if working:
        working_turn(child.name)
    assert not any(todo.pending for todo in comms.goals.todos.held_by(
        comms.registry.require("lead").incarnation, child.incarnation))
    with LaunchRoot.from_path(comms.root).selected():
        assert Load.of(comms, "lead").forks == expected


def test_capacity_does_not_mix_new_children_into_a_captured_frame(held_bus, working_turn):
    from agent_comms.todos import PullRequests
    from agent_comms.ui_model.unit_state import UnitState

    states = UnitState.current(held_bus, "lead", PullRequests())
    child = held_bus.registry.new_thread(
        "late-fork", frozenset(), str(held_bus.root), parent=ForkedParent("lead"),
        process_identity=ProcessIdentity.capture(os.getpid()))
    held_bus.registry.register(child, RunningThreadStatus())
    working_turn(child.name)
    assert "late-fork" not in Load.from_states(held_bus, "lead", states).forks
    assert "late-fork" in Load.of(held_bus, "lead").forks


