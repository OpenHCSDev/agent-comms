"""Committed notifications survive a new watcher and use the real publisher/readers."""

import os
import pickle
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.commander_heartbeat import CommanderHeartbeat, CommanderObservation
from agent_comms.commander_hud import CommanderFrame, CommanderHud, CodexUsage, UnreadableUsage
from agent_comms.commander_pings import CommanderDecisions, CommanderNotice, CommanderUpdate, RulePing
from agent_comms.errors import OverdueHeadRefusalError, UnregisteredThreadError
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.launch_root import LaunchRoot
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import DMDisplayScope
from agent_comms.service_passes import ServicePassStore
from agent_comms.todos import ProgressRead
from agent_comms.ui_model.observation import CommanderHudObserved, ObserveCommanderHud
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


def lead_hud(comms, states=()):
    return CommanderHud.of(comms, "lead", HeartbeatFrame.read(comms, "lead", ProgressRead()), states)


def beat(comms, rules=(), *, states=()):
    frame = CommanderObservation(
        comms.registry.require("lead"), (), rules, states, hud=lead_hud(comms, states))
    # Deliberately construct a fresh watcher on every pass, never reuse a cache.
    CommanderHeartbeat().pass_body(comms, (frame,))


def test_frame_puts_hands_first_and_folds_finished_phases_per_plan(notification_bus):
    """Ready and done-but-held phases lead; finished phases are one count per plan."""
    from agent_comms.commander.vibe_check import OverloadDegradation
    from agent_comms.commander_hud import CommanderPhase
    from agent_comms.plan_phases import (
        DesignReconciledPhase, OneOrderOwnerPhase, RecordPlanePhase, RetainedStorePhase,
        ReviewQueuePhase,
    )

    comms = notification_bus
    lead = comms.registry.require("lead").incarnation
    worker = comms.registry.require("worker").incarnation
    hud = CommanderHud(lead, (), (
        CommanderPhase(RetainedStorePhase, True, False, ()),
        CommanderPhase(OneOrderOwnerPhase, True, False, ()),
        CommanderPhase(DesignReconciledPhase, False, False, (worker,)),
        CommanderPhase(RecordPlanePhase, False, True, ()),
        CommanderPhase(ReviewQueuePhase, True, False, (worker,)),
    ), OverloadDegradation.from_orders((), 0, 0), (worker,), CodexUsage(UnreadableUsage('not requested')))
    assert hud.frame == (
        str(hud.load),
        "commander_native/record_plane done=False ready=True holders=",
        "commander_native/review_queue done=True ready=False holders=worker",
        "commander_native/design_reconciled done=False ready=False holders=worker",
        "usage unreadable: not requested",
        "forks: worker",
        "commander_native: 2 phases done",
    )


def test_a_registered_commander_fork_is_on_the_hud(notification_bus):
    comms = notification_bus
    fork = comms.registry.register_commander_child("judge-fork", "lead", comms.goals)
    assert fork.incarnation in lead_hud(comms).forks


def test_quiet_completion_is_per_commander_even_when_a_later_recipient_fails(notification_bus):
    comms = notification_bus
    lead = comms.registry.require("lead")
    missing = replace(comms.registry.require("worker"), name="unregistered-recipient")
    frames = tuple(CommanderObservation(thread, (), (), (), hud=lead_hud(comms))
                   for thread in (lead, missing))
    with pytest.raises(UnregisteredThreadError):
        CommanderHeartbeat().pass_body(comms, frames)
    completed = ServicePassStore.at(comms.root).read().commander_completed
    assert set(completed) == {lead.incarnation}
    assert missing.incarnation not in completed
    # A quiet pass still completes after its original notice was committed.
    beat(comms)
    assert len(notifications(comms)) == 1
    # A pass that misses a still-registered commander retires nothing but its frame.
    CommanderHeartbeat().pass_body(comms, ())
    assert CommanderFrame.read_all(comms.root) == ()
    assert set(ServicePassStore.at(comms.root).read().commander_completed) == {lead.incarnation}
    # Only the registry retiring the incarnation drops its completion.
    comms.registry.unregister("lead")
    comms.registry.remove("lead")
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


def test_overdue_head_refusal_is_published_and_later_rule_still_runs(notification_bus, monkeypatch):
    comms = notification_bus
    enforced = []

    def enforce(rule, _comms, _commander):
        enforced.append(rule.watch)
        if rule.watch == "overdue-head":
            raise OverdueHeadRefusalError("Review overdue heads before sending new orders:\ntoad#1176")

    monkeypatch.setattr(ServicePassFailing, "enforce", enforce)
    rules = (ServicePassFailing("overdue-head", 1.0, "review pending"),
             ServicePassFailing("following-rule", 1.0, "another rule"))
    beat(comms, rules)

    assert enforced == ["overdue-head", "following-rule"]
    message, = notifications(comms)
    assert "Not applied (a reviewed head is overdue):" in message.body
    assert rules[0].line() in message.body
    assert RulePing.capture(rules[0]) not in message.task.handled
    assert RulePing.capture(rules[1]) in message.task.handled
    assert comms.registry.require("lead").incarnation in (
        ServicePassStore.at(comms.root).read().commander_completed)


def test_rule_packets_list_every_new_line_and_never_restate_one(notification_bus):
    comms = notification_bus
    rules = tuple(ServicePassFailing(f"watch-{index}", 1.0, "failed source read")
                  for index in range(7))
    beat(comms, rules)
    first, = notifications(comms)
    assert isinstance(first.task, CommanderDecisions)
    assert set(first.task.decisions) == set(first.task.handled) == {
        RulePing.capture(rule) for rule in rules}
    assert first.body.splitlines() == [f"{rule.line()} (0 min)" for rule in rules]
    beat(comms, rules)
    assert len(notifications(comms)) == 1
    # Changed diagnostic wording is not another failed occurrence.
    beat(comms, tuple(replace(rule, error="new diagnostic detail") for rule in rules))
    assert len(notifications(comms)) == 1


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
    # Nothing new and nothing cleared sends nothing.
    beat(comms, ServicePassFailing.violations(comms, "lead"))
    assert len(notifications(comms)) == 2
    # Use the same source occurrence to prove it was retired, not merely hidden.
    beat(comms, rules)
    _, _, repeated = notifications(comms)
    assert repeated.task.decisions == first.task.decisions


def test_latest_observation_follows_commander_and_service_rename_aliases(notification_bus):
    comms = notification_bus
    beat(comms, (ServicePassFailing("watch", 1.0, "failed source read"),))
    first, = notifications(comms)
    comms.threads._rename_thread("lead", "renamed-lead")
    comms.threads._rename_thread(CommanderHeartbeat.sender, "renamed-heartbeat")
    previous = CommanderUpdate.latest(comms, comms.registry.require("renamed-lead").incarnation)
    assert previous == first.task


def test_new_local_commit_on_an_unpushed_branch_is_not_a_resolution(notification_bus, tmp_path):
    from agent_comms.commander.publication_rules import UnpushedBranch
    from agent_comms.work_branches import UnpushedHead

    comms = notification_bus
    holder = comms.registry.require("worker").incarnation
    first, moved = (UnpushedBranch("P594", "order", holder, tmp_path,
                                   UnpushedHead(head, commits, ""))
                    for head, commits in (("a" * 40, 54), ("b" * 40, 57)))
    assert RulePing.capture(first) == RulePing.capture(moved)
    beat(comms, (first,))
    beat(comms, (moved,))
    assert len(notifications(comms)) == 1


def test_commander_page_reads_each_beats_frame_and_nothing_between(notification_bus):
    comms = notification_bus
    states = state_cards(comms, UnitState)
    reading = ObserveCommanderHud().observation()
    beat(comms, states=states)
    observed, = reading.read(comms, comms.views.revision())
    # The result crosses the observation process boundary by pickle.
    assert pickle.loads(pickle.dumps(observed)).huds == (lead_hud(comms, states),)
    assert not list(reading.read(comms, comms.views.revision()))
    beat(comms, states=states)
    assert isinstance(next(reading.read(comms, comms.views.revision())), CommanderHudObserved)


def test_codex_usage_decodes_window_and_keeps_refusal_visible():
    import json
    from datetime import datetime
    from agent_comms.cli_bridges.codex_protocol import RateLimitsResponse
    from agent_comms.field_codec import FieldCodec

    reply = FieldCodec.decode(RateLimitsResponse, json.loads('''{
        "rateLimits": {"primary": {
            "usedPercent": 37, "windowDurationMins": 10080, "resetsAt": 1793513456
        }}
    }'''))
    usage = CodexUsage(reply.rate_limits)
    reset = datetime.fromtimestamp(1793513456).strftime('%a %b %d %H:%M')
    assert usage.line() == f'usage 37% weekly, resets {reset}'
    assert not usage.needs_hand()
    assert FieldCodec.decode(CodexUsage, FieldCodec.encode(usage)) == usage
    refused = CodexUsage(UnreadableUsage('Codex: account unavailable'))
    assert refused.line() == 'usage unreadable: Codex: account unavailable'
    assert refused.needs_hand()
    assert FieldCodec.decode(CodexUsage, FieldCodec.encode(refused)) == refused
