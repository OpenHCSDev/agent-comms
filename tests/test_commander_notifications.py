"""Committed notifications survive a new watcher and use the real publisher/readers."""

import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.commander_heartbeat import CommanderHeartbeat, CommanderObservation
from agent_comms.commander_pings import CommanderDecisions, CommanderNotice, CommanderUpdate, RulePing
from agent_comms.errors import UnregisteredThreadError
from agent_comms.launch_root import LaunchRoot
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import DMDisplayScope
from agent_comms.service_passes import ServicePassStore
from agent_comms.standing_rules import ServicePassFailing
from agent_comms.ui_model.unit_state import UnitState
from agent_comms.wake_policy import PassiveWake
from test_holder_standing_rules import state_cards

pytest_plugins = ("test_holder_standing_rules",)


@pytest.fixture
def notification_bus(held_bus, installed_runtime):
    lead = held_bus.registry.require("lead")
    held_bus.registry.register(replace(lead, process_identity=ProcessIdentity.capture(os.getpid())))
    CommanderHeartbeat.register(held_bus.registry)
    with LaunchRoot.from_path(held_bus.root).selected():
        yield held_bus


def notifications(comms):
    page = MessagePageRequest(DMDisplayScope.capture(
        CommanderHeartbeat.sender, "lead", comms.registry.snapshot(),
    )).read_opened(comms.bus.bus)
    return tuple(message for message in page.messages if isinstance(message.task, CommanderUpdate))


def beat(comms, rules=(), *, states=()):
    frame = CommanderObservation(comms.registry.require("lead"), (), rules, states)
    # Deliberately construct a fresh watcher on every pass, never reuse a cache.
    CommanderHeartbeat().pass_body(comms, (frame,))


def test_quiet_completion_is_per_commander_even_when_a_later_recipient_fails(notification_bus):
    comms = notification_bus
    lead = comms.registry.require("lead")
    missing = replace(comms.registry.require("worker"), name="unregistered-recipient")
    frames = tuple(CommanderObservation(thread, (), (), ()) for thread in (lead, missing))
    with pytest.raises(UnregisteredThreadError):
        CommanderHeartbeat().pass_body(comms, frames)
    completed = ServicePassStore.at(comms.root).read().commander_completed
    assert set(completed) == {lead.incarnation}
    assert missing.incarnation not in completed
    # A quiet pass still completes after its original notice was committed.
    beat(comms)
    assert len(notifications(comms)) == 1
    CommanderHeartbeat().pass_body(comms, ())
    assert not ServicePassStore.at(comms.root).read().commander_completed


def test_recreated_watcher_reads_original_committed_unit_observation(notification_bus):
    comms = notification_bus
    states = state_cards(comms, UnitState)
    beat(comms, states=states)
    first, = notifications(comms)
    assert all(state.line() in first.body for state in states)
    assert CommanderUpdate.latest(comms, comms.registry.require("lead").incarnation) == first.task
    beat(comms, states=state_cards(comms, UnitState))
    unchanged, = notifications(comms)
    assert unchanged.reference == first.reference


def test_rule_packets_admit_five_and_leave_remaining_occurrences_unhandled(notification_bus):
    comms = notification_bus
    rules = tuple(ServicePassFailing(f"watch-{index}", 1.0, "failed source read")
                  for index in range(7))
    beat(comms, rules)
    first, = notifications(comms)
    assert isinstance(first.task, CommanderDecisions)
    assert len(first.task.decisions) == 5
    beat(comms, rules)
    first, second = notifications(comms)
    assert isinstance(second.task, CommanderDecisions)
    assert len(second.task.decisions) == 2
    assert {RulePing.capture(rule) for rule in rules} == set(second.task.handled)
    beat(comms, rules)
    assert len(notifications(comms)) == 2
    # Changed diagnostic wording is not another failed occurrence.
    beat(comms, tuple(replace(rule, error="new diagnostic detail") for rule in rules))
    assert len(notifications(comms)) == 2


def test_resolution_is_passive_and_same_rule_can_recur(notification_bus):
    comms = notification_bus
    passes = ServicePassStore.at(comms.root)
    passes.failed("dependent-watch", RuntimeError("unreadable source"))
    rules = ServicePassFailing.violations(comms, "lead")
    beat(comms, rules)
    first, = notifications(comms)
    passes.passed("dependent-watch")
    beat(comms, ServicePassFailing.violations(comms, "lead"))
    _, quiet = notifications(comms)
    assert isinstance(quiet.task, CommanderNotice)
    assert all(isinstance(decision.wake_mode, PassiveWake) for decision in quiet.decisions)
    # Use the same source occurrence to prove it was retired, not merely hidden.
    beat(comms, rules)
    _, _, repeated = notifications(comms)
    assert repeated.task.decisions == first.task.decisions


def test_latest_observation_follows_commander_and_service_rename_aliases(notification_bus):
    comms = notification_bus
    beat(comms)
    first, = notifications(comms)
    comms.threads._rename_thread("lead", "renamed-lead")
    comms.threads._rename_thread(CommanderHeartbeat.sender, "renamed-heartbeat")
    previous = CommanderUpdate.latest(comms, comms.registry.require("renamed-lead").incarnation)
    assert previous == first.task
