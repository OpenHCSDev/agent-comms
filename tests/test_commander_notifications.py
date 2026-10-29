"""Committed notifications survive a new watcher and use the real publisher/readers."""

import asyncio
import json
import os
import pickle
import sqlite3
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from multiprocessing import Pipe

import pytest

from agent_comms.registry_document import RegisteredCommanderProject

from agent_comms.child_process import ProcessIdentity
from agent_comms.commander_heartbeat import CommanderHeartbeat, CommanderObservation
from agent_comms.comms import Comms
from commander_heartbeat_fixture import ObservedHeartbeat, capture_frame, current_hud
from agent_comms.commander_hud import (
    CommanderFrame, CommanderHud, CodexUsage, FinishedPlansGroup, UnreadableUsage,
)
from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.task_attachments.commander_pings import CommanderDecisions, CommanderNotice, CommanderUpdate, RulePing
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.launch_root import LaunchRoot
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import DMDisplayScope
from agent_comms.service_passes import ServicePassStore
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import ProgressRead, UnitOccupied
from agent_comms.ui_model import observation
from agent_comms.ui_model.observation import CommanderHudObserved, ObservationService, ObserveCommanderHud
from agent_comms.ui_model.observation import ReadCommanderUnit, ReadRefused
from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.standing_rules import ReviewTimerUp, ServicePassFailing, StandingRule, UndatedStandingRule
from agent_comms.ui_model.unit_state import (
    Spinning, UnitState, UrgentGroup,
)
from agent_comms.wake_policy import PassiveWake
from agent_comms.bus_publication import AgentOrigin
from agent_comms.channel_targets import ThreadTarget
from agent_comms.messages import ConversationRole, InfoKind, Message
from agent_comms.threads import Thread
from test_holder_standing_rules import state_cards
from test_unit_state import recorded_turn
from agent_comms.thread_execution import CommanderForkExecution

pytest_plugins = ("test_holder_standing_rules",)


@pytest.fixture
def notification_bus(held_bus, installed_runtime, monkeypatch, tmp_path):
    # This private bus has no account server. Exercise the actual unavailable
    # endpoint rather than querying the operator's live account on every beat.
    monkeypatch.setattr(CodexConnection, "CONTROL_SOCKET", tmp_path / "no-account.sock")
    lead = held_bus.registry.require("lead")
    held_bus.registry.register(replace(lead, process_identity=ProcessIdentity.capture(os.getpid())))
    CommanderHeartbeat.register(held_bus.registry)
    with LaunchRoot.from_path(held_bus.root).selected():
        yield held_bus


def test_real_heartbeat_dispatches_watch_and_plan_roles_separately(notification_bus: Comms) -> None:
    heartbeat = CommanderHeartbeat()
    prepared = heartbeat.prepare(notification_bus)
    lead = notification_bus.registry.require("lead")
    inputs = prepared[lead]
    observed = heartbeat.observe(notification_bus, prepared)
    item = next(item for item in observed if item.thread == lead)
    assert item.frame.plan is inputs.plan
    assert item.frame.pulls is inputs.pulls
    assert (item.frame.live, item.frame.orphaned) == (inputs.live, inputs.orphaned)
    assert inputs.plan.reads
    assert any(plan.main == inputs.plan.main for plan in heartbeat.plans)


def notifications(comms):
    page = MessagePageRequest(DMDisplayScope.capture(
        CommanderHeartbeat.sender, "lead", comms.registry.snapshot(),
    )).read_opened(comms.bus.bus)
    return tuple(message for message in page.messages if isinstance(message.task, CommanderUpdate))


def lead_hud(comms, states=()):
    return current_hud(comms, "lead", capture_frame(comms, "lead", ProgressRead()), states)


def assert_status_fields(message, hud):
    facts = hud.status_table(time.time()).facts
    assert all(f"{label}: {value}" in message.body
               for row in facts.rows for label, value in zip(facts.columns, row))


def beat(comms, rules=(), *, states=()):
    observed = CommanderObservation(
        comms.registry.require("lead"), capture_frame(comms, "lead", ProgressRead()),
        rules, states)
    # Deliberately construct a fresh watcher on every pass, never reuse a cache.
    asyncio.run(ObservedHeartbeat((observed,)).run(lambda: nullcontext(comms)))
    return CommanderFrame.current(comms, "lead")


def test_expanded_unit_reads_original_orders_and_bounded_activity_then_refuses_stale_selection(
    notification_bus,
):
    comms = notification_bus
    recorded_turn(comms, (False,) * 10)
    original = (comms.root / 'state-source.jsonl').read_bytes()
    hud = beat(comms, states=state_cards(comms, UnitState))
    ServicePassStore.at(comms.root).heartbeat_passed((hud.commander,), (hud.commander,))
    card = next(card for card in hud.units if card.unit.name == 'worker')
    request = ReadCommanderUnit(str(comms.root), hud.commander, card.unit,
                               tuple(dict.fromkeys(order.order for order in card.orders)))
    detail = pickle.loads(pickle.dumps(request.read(comms)))
    assert detail.unit == card
    assert {order.card.problem for order in detail.orders} == {'P1', 'P2'}
    for order in detail.orders:
        todo = comms.goals.todos.get(order.card.problem)
        assert order.instructions == (todo.order.instructions(todo),)
        assert {problem.problem for problem in order.story.problems} >= {todo.id}
    turn, = detail.activity['turns']
    assert len(turn['calls_started']) == 6
    assert 'contents' not in json.dumps(detail.activity)
    assert (comms.root / 'state-source.jsonl').read_bytes() == original
    for stale in (
        replace(request, orders=()),
        replace(request, root=str(comms.root / 'other')),
        replace(request, unit=replace(card.unit, created_at=card.unit.wire_birth + 1)),
        replace(request, commander=card.unit),
    ):
        assert isinstance(stale.answer(comms), ReadRefused)
    checkout = comms.goals.todos.get('P1').order.checked().check.directory
    metadata = checkout / '.git'
    saved = checkout / 'saved-git'
    metadata.rename(saved)
    try:
        metadata.write_text('gitdir: ./missing-original-git\n')
        assert isinstance(request.answer(comms), ReadRefused)
    finally:
        metadata.unlink()
        saved.rename(metadata)


class BrokenHeartbeat(CommanderHeartbeat, declared_name=None):
    def observe(self, comms, prepared):
        raise RuntimeError("forced heartbeat pass failure")


def failed_pass(comms, error):
    """One failed pass in the coordinator's order: the store records it, then a fresh
    watcher's callback runs; the original exception still escapes the pass."""
    heartbeat = BrokenHeartbeat()
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(heartbeat.run(lambda: nullcontext(comms)))
    ServicePassStore.at(comms.root).failed(BrokenHeartbeat.__name__, error)
    heartbeat.failed(lambda: nullcontext(comms), error)
    return raised.value


def retained_failure(comms):
    stored = ServicePassStore.at(comms.root).read().failing[BrokenHeartbeat.__name__]
    return ServicePassFailing(BrokenHeartbeat.__name__, stored.since, stored.error)


def aged(pings):
    return tuple((ping, ping.since) for ping in pings)


def test_failed_heartbeat_pass_publishes_one_typed_packet_and_still_raises(notification_bus):
    comms = notification_bus
    raised = failed_pass(comms, RuntimeError("first wording"))
    assert str(raised) == "forced heartbeat pass failure"
    failure = retained_failure(comms)
    message, = notifications(comms)
    assert message.sender == CommanderHeartbeat.sender and message.target == "lead"
    assert isinstance(message.task, CommanderDecisions)
    assert message.task.decisions == message.task.handled == (RulePing.capture(failure),)
    assert failure.line() in message.body and "first wording" in message.body
    # The same occurrence again, from a fresh watcher with new wording, is not new.
    failed_pass(comms, ValueError("second wording"))
    assert retained_failure(comms).since == failure.since
    assert len(notifications(comms)) == 1


def test_failure_keeps_the_board_and_recovery_clears_only_the_failure(notification_bus):
    comms = notification_bus

    class RefusedRule(ServicePassFailing):
        def enforce(self, _comms, _commander):
            raise UnitOccupied("another order already holds the selected unit")

    recorded_turn(comms, (True, True))
    states = state_cards(comms, Spinning)
    rules = (RefusedRule("refused", 1.0, "review pending"),
             ServicePassFailing("independent", 1.0, "another source"))
    beat(comms, rules, states=states)
    board, = notifications(comms)
    assert board.task.not_applied and len(board.task.handled) == 2

    failed_pass(comms, RuntimeError("source unreadable"))
    _, failing = notifications(comms)
    failure = RulePing.capture(retained_failure(comms))
    assert isinstance(failing.task, CommanderDecisions)
    assert aged(failing.task.handled[:-1]) == aged(board.task.handled)
    assert failing.task.handled[-1] == failure and failing.task.decisions == (failure,)
    assert failing.task.waiting == ()
    assert failing.task.not_applied == board.task.not_applied

    ServicePassStore.at(comms.root).passed(BrokenHeartbeat.__name__)
    beat(comms, rules, states=state_cards(comms, Spinning))
    _, _, recovered = notifications(comms)
    assert isinstance(recovered.task, CommanderDecisions)
    assert recovered.task.decisions == ()
    assert aged(recovered.task.handled) == aged(board.task.handled)

    # A new occurrence after the pass recovered is new, once.
    failed_pass(comms, RuntimeError("source unreadable again"))
    *_, again = notifications(comms)
    assert again.task.decisions == (RulePing.capture(retained_failure(comms)),)
    assert again.task.decisions != (failure,)
    failed_pass(comms, RuntimeError("source unreadable again"))
    assert len(notifications(comms)) == 4


def test_each_commander_gets_its_own_failure_packet(notification_bus):
    comms = notification_bus
    ServicePassStore.at(comms.root).failed(BrokenHeartbeat.__name__, RuntimeError("down"))
    failure = retained_failure(comms)
    now = time.time()
    for name in ("lead", "worker"):
        commander = comms.registry.require(name).incarnation
        update, = CommanderUpdate.report_failure(commander, failure, (), now=now)
        update.send(comms, now, ())
        latest, = CommanderUpdate.latest(comms, commander)
        assert latest.commander == commander
        assert latest.handled == (RulePing.capture(failure),)
        assert CommanderUpdate.report_failure(commander, failure, (latest,), now=now + 1) == ()
    # The worker's later packet is not the lead's board.
    lead = comms.registry.require("lead").incarnation
    lead_board, = CommanderUpdate.latest(comms, lead)
    assert lead_board.commander == lead


def independent_instruction(comms, body):
    """A taskless message from the heartbeat's own sender, as raw alarms were published."""
    return comms.bus.publisher.publish_ordinary(
        Message(sender=CommanderHeartbeat.sender, target="lead", body=body,
                type=InfoKind, role=ConversationRole()),
        origin=AgentOrigin(), destination=ThreadTarget("lead"),
    )


def everything_to_lead(comms):
    page = MessagePageRequest(DMDisplayScope.capture(
        CommanderHeartbeat.sender, "lead", comms.registry.snapshot(),
    )).read_opened(comms.bus.bus)
    return {message.reference for message in page.messages}


def test_hidden_tool_time_does_not_append_quiet_bus_history(notification_bus):
    comms = notification_bus
    recorded_turn(comms, (False,))
    hud = beat(comms, states=state_cards(comms, UnitState))
    first, = notifications(comms)
    assert isinstance(first.task, CommanderNotice)
    for _ in range(2):
        recorded_turn(comms, (False,))
        later = beat(comms, states=state_cards(comms, UnitState))
        assert tuple(card.clock for card in hud.units) != tuple(card.clock for card in later.units)
        assert hud.display_facts == later.display_facts
    assert tuple(message.reference for message in notifications(comms)) == (first.reference,)


def test_unchanged_open_alarm_wakes_each_beat_until_a_recorded_action(notification_bus):
    from test_holder_standing_rules import publish

    comms = notification_bus
    todo = comms.goals.todos.get("P1")
    rule = ReviewTimerUp(problem=todo.id, holder=todo.held().owner_identity,
                         order=todo.order.checked().id, since=RecordedEventTime(time.time()))
    beat(comms, (rule,))
    first, = notifications(comms)
    beat(comms, (rule,))
    _, second = notifications(comms)
    assert isinstance(second.task, CommanderDecisions)
    assert second.task.decisions == ()
    assert second.task.handled[0].since == first.task.handled[0].since
    hud = CommanderFrame.current(comms, "lead")
    assert hud.board == second.task.board(comms, "lead")
    assert len(hud.board) == 1

    publish(comms, "lead", "worker", time.time())
    beat(comms, (rule,))
    _, _, cleared = notifications(comms)
    assert isinstance(cleared.task, CommanderNotice)
    assert CommanderFrame.current(comms, "lead").board == ()
    beat(comms, (rule,))
    assert len(notifications(comms)) == 3


def test_an_independent_heartbeat_message_never_replaces_the_board(notification_bus):
    comms = notification_bus
    failing = ServicePassFailing("following-rule", time.time(), "independent source")
    beat(comms, (failing,))
    board, = notifications(comms)
    raw = independent_instruction(comms, "Heartbeat pass failed: an old raw alarm")
    lead = comms.registry.require("lead").incarnation
    assert CommanderUpdate.latest(comms, lead) == (board.task,)
    assert CommanderUpdate.current_board(comms, "lead") == board.task.board(comms, "lead")
    assert lead_hud(comms).board == board.task.board(comms, "lead")

    assert raw.reference in everything_to_lead(comms)
    # The next pass clears NEW without promoting a subjectless observation to STILL OPEN.
    beat(comms, (failing,))
    _, repeated = notifications(comms)
    assert isinstance(repeated.task, CommanderNotice)
    assert repeated.task.handled == board.task.handled


def test_a_later_notice_is_the_baseline_and_keeps_the_lines_still_on_it(notification_bus):
    comms = notification_bus
    kept = ServicePassFailing("kept-rule", time.time(), "still failing")
    cleared = ServicePassFailing("cleared-rule", time.time(), "now fixed")
    beat(comms, (kept, cleared))
    decisions, = notifications(comms)
    beat(comms, (kept,))
    _, notice = notifications(comms)
    assert isinstance(notice.task, CommanderNotice)
    assert CommanderUpdate.latest(comms, comms.registry.require("lead").incarnation) == (notice.task,)
    assert RulePing.capture(kept) in notice.task.handled
    assert RulePing.capture(cleared) not in notice.task.handled
    assert decisions.reference in everything_to_lead(comms)


def test_a_raw_message_between_failures_does_not_restate_the_board(notification_bus):
    comms = notification_bus
    rules = (ServicePassFailing("independent", 1.0, "another source"),)
    beat(comms, rules)
    board, = notifications(comms)
    failed_pass(comms, RuntimeError("down"))
    independent_instruction(comms, "Heartbeat pass failed: down")
    failed_pass(comms, RuntimeError("still down"))
    assert len(notifications(comms)) == 2
    ServicePassStore.at(comms.root).passed(BrokenHeartbeat.__name__)
    beat(comms, rules)
    *_, recovered = notifications(comms)
    assert isinstance(recovered.task, CommanderNotice)
    assert aged(recovered.task.handled) == aged(board.task.handled)
    assert recovered.task.not_applied == board.task.not_applied


def test_a_recreated_commander_inherits_no_board(notification_bus):
    comms = notification_bus
    failing = ServicePassFailing("following-rule", time.time(), "independent source")
    beat(comms, (failing,))
    board, = notifications(comms)
    worktree = comms.registry.require("lead").worktree
    comms.registry.unregister("lead")
    comms.registry.remove("lead")
    comms.registry.declare(Thread("lead", frozenset({"comms"}), worktree))
    reborn = comms.registry.require("lead").incarnation
    assert reborn != board.task.commander
    assert CommanderUpdate.latest(comms, reborn) == ()
    assert CommanderUpdate.latest(comms, board.task.commander) == (board.task,)
    now = time.time()
    update, = CommanderUpdate.report_failure(reborn, failing, (), now=now)
    update.send(comms, now, ())
    latest, = CommanderUpdate.latest(comms, reborn)
    assert latest.commander == reborn and latest.handled == update.handled


def test_only_the_heartbeat_publishes_a_packet_for_its_exact_commander(notification_bus):
    comms = notification_bus
    lead = comms.registry.require("lead").incarnation
    worker = comms.registry.require("worker").incarnation
    for sender, target, commander in (("worker", "lead", lead),
                                      (CommanderHeartbeat.sender, "lead", worker)):
        with pytest.raises(RelationViolationError):
            comms.bus.publisher.publish_ordinary(
                Message(sender=sender, target=target, body="forged board", type=InfoKind,
                        role=CommanderNotice.role, task=CommanderNotice(commander, ())),
                origin=AgentOrigin(), destination=ThreadTarget(target),
            )
    assert CommanderUpdate.latest(comms, lead) == ()
    assert CommanderUpdate.latest(comms, worker) == ()


def test_failure_packet_follows_commander_and_service_rename_aliases(notification_bus):
    comms = notification_bus
    failed_pass(comms, RuntimeError("down"))
    first, = notifications(comms)
    comms.threads._rename_thread("lead", "renamed-lead")
    comms.threads._rename_thread(CommanderHeartbeat.sender, "renamed-heartbeat")
    failed_pass(comms, RuntimeError("still down"))
    page = MessagePageRequest(DMDisplayScope.capture(
        "renamed-heartbeat", "renamed-lead", comms.registry.snapshot(),
    )).read_opened(comms.bus.bus)
    published = tuple(message for message in page.messages
                      if isinstance(message.task, CommanderUpdate))
    assert tuple(message.reference for message in published) == (first.reference,)


def test_frame_puts_hands_first_and_folds_finished_phases_per_plan(notification_bus):
    """Ready and done-but-held phases lead; finished phases are one count per plan."""
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
    ), OverloadDegradation.from_orders((), 0, 0), (worker,), CodexUsage(UnreadableUsage(reason='not requested')),
        open_pings=())
    urgent = next(section for section in hud.groups if section.group is UrgentGroup)
    assert tuple(row.phase for row in urgent.records) == (RecordPlanePhase, ReviewQueuePhase)
    assert urgent.tables[0].record is type(hud.phases[0])
    folded, = next(section.records for section in hud.groups
                    if section.group is FinishedPlansGroup)
    assert folded.phases == (RetainedStorePhase, OneOrderOwnerPhase)
    assert folded.done == 2
    rendered = "\n".join(hud.hud_text(time.time()))
    assert all(phase.declared_name in rendered for phase in (RecordPlanePhase, ReviewQueuePhase))


def test_a_registered_commander_fork_is_on_the_hud(notification_bus):
    comms = notification_bus
    fork = comms.registry.register_child("judge-fork", "lead", CommanderForkExecution, comms.goals)
    assert fork.incarnation in lead_hud(comms).forks


def test_quiet_completion_is_per_commander_even_when_a_later_recipient_fails(notification_bus):
    comms = notification_bus
    lead = comms.registry.require("lead")
    missing = replace(comms.registry.require("worker"), name="unregistered-recipient")
    failing = ServicePassFailing("following-rule", time.time(), "independent source")
    frame = capture_frame(comms, "lead", ProgressRead())
    frames = tuple(CommanderObservation(thread, frame, (failing,), ())
                   for thread in (lead, missing))
    with pytest.raises(UnregisteredThreadError):
        asyncio.run(ObservedHeartbeat(frames).run(lambda: nullcontext(comms)))
    completed = ServicePassStore.at(comms.root).read().commander_completed
    assert set(completed) == {lead.incarnation}
    assert missing.incarnation not in completed
    # A quiet pass still completes and keeps the original alarm visibly open.
    beat(comms, (failing,))
    assert len(notifications(comms)) == 2
    # A pass that misses a still-registered commander retires nothing but its frame.
    asyncio.run(ObservedHeartbeat(()).run(lambda: nullcontext(comms)))
    assert CommanderFrame.read_all(comms.root) == ()
    assert set(ServicePassStore.at(comms.root).read().commander_completed) == {lead.incarnation}
    # Only the registry retiring the incarnation drops its completion.
    comms.registry.unregister("lead")
    comms.registry.remove("lead")
    asyncio.run(ObservedHeartbeat(()).run(lambda: nullcontext(comms)))
    assert not ServicePassStore.at(comms.root).read().commander_completed


def test_recreated_watcher_retains_non_disposable_observation_without_nagging(notification_bus):
    """The original observation keeps its age, but is not an actionable open board line."""
    comms = notification_bus
    states = state_cards(comms, UnitState)
    assert states and not any(state.attention() for state in states)
    failing = ServicePassFailing("following-rule", time.time(), "independent source")
    beat(comms, (failing,), states=states)
    first, = notifications(comms)
    assert type(failing).declared_name in first.body
    assert not any(state.line() in first.body for state in states)
    hud = lead_hud(comms, states)
    assert_status_fields(first, hud)
    assert all(section.heading() in first.body for section in hud.groups)
    assert CommanderUpdate.latest(comms, comms.registry.require("lead").incarnation) == (first.task,)
    beat(comms, (failing,), states=state_cards(comms, UnitState))
    _, unchanged = notifications(comms)
    assert unchanged.reference != first.reference
    assert isinstance(unchanged.task, CommanderNotice)
    assert aged(unchanged.task.handled) == aged(first.task.handled)
    assert lead_hud(comms).board == ()
    from agent_comms.task_attachments.commander_pings import WatchingAlarms

    section, = CommanderFrame.current(comms, "lead").alarms
    assert section.group is WatchingAlarms
    assert tuple(ping.text for ping in section.pings) == (failing.line(),)


def test_occupied_unit_refusal_is_published_and_later_rule_still_runs(notification_bus):
    comms = notification_bus
    enforced = []
    refusal = UnitOccupied("another order already holds the selected unit")

    class FollowingRule(ServicePassFailing):
        def enforce(self, _comms, _commander):
            enforced.append(self.watch)

    class OccupiedRule(FollowingRule):
        def enforce(self, _comms, _commander):
            super().enforce(_comms, _commander)
            raise refusal

    rules = (OccupiedRule("occupied-unit", 1.0, "unit already holds work"),
             FollowingRule("following-rule", 1.0, "another rule"))
    beat(comms, rules)

    assert enforced == ["occupied-unit", "following-rule"]
    message, = notifications(comms)
    assert len(message.task.not_applied) == 1
    assert refusal.explanation() in message.body
    assert rules[0].line() in message.body
    assert RulePing.capture(rules[0]) not in message.task.handled
    assert RulePing.capture(rules[1]) in message.task.handled
    assert comms.registry.require("lead").incarnation in (
        ServicePassStore.at(comms.root).read().commander_completed)


def test_rule_packets_compact_new_lines_keep_originals_then_repeat_without_a_new_wake(notification_bus):
    comms = notification_bus
    rules = tuple(ServicePassFailing(f"watch-{index}", 1.0, "failed source read")
                  for index in range(7))
    beat(comms, rules)
    first, = notifications(comms)
    assert isinstance(first.task, CommanderDecisions)
    assert set(first.task.decisions) == set(first.task.handled) == {
        RulePing.capture(rule) for rule in rules}
    assert sum(len(section.pings) for section in lead_hud(comms).alarms) == len(rules)
    assert rules[0].line() in first.body
    details = "\n".join(lead_hud(comms).alarm_detail(lead_hud(comms).alarms, time.time()))
    assert all(rule.line() in details for rule in rules)
    assert_status_fields(first, lead_hud(comms))
    beat(comms, rules)
    _, unchanged = notifications(comms)
    assert isinstance(unchanged.task, CommanderNotice)
    assert CommanderFrame.current(comms, "lead").board == ()
    # Changed diagnostic wording is visible but not another failed occurrence.
    beat(comms, tuple(replace(rule, error="new diagnostic detail") for rule in rules))
    assert len(notifications(comms)) == 3
    changed = CommanderFrame.current(comms, "lead")
    assert aged(changed.packets[0].handled) == aged(first.task.handled)
    assert all("new diagnostic detail" in ping.text for ping in changed.packets[0].handled)


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
    assert_status_fields(quiet, lead_hud(comms))
    assert all(isinstance(decision.wake_mode, PassiveWake) for decision in quiet.decisions)
    # An identical quiet frame updates no bus history.
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
    assert previous == (first.task,)


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
    original, repeated = notifications(comms)
    assert isinstance(repeated.task, CommanderDecisions)
    assert repeated.task.decisions == ()
    assert aged(repeated.task.handled) == aged(original.task.handled)


def test_failed_rule_scan_raises_to_the_heartbeat(notification_bus, request):
    comms = notification_bus
    frame = capture_frame(comms, "lead", ProgressRead())
    states = UnitState.current(comms, "lead", frame)
    @dataclass(frozen=True)
    class FailedScan(UndatedStandingRule):
        @classmethod
        def scan_observed(cls, comms, commander, observed, frame):
            raise IndexError("P729 isolated scan failure")

        def line(self):
            return "unreachable scan result"

    request.addfinalizer(lambda: StandingRule.__registry__.pop(FailedScan.declared_name))
    with pytest.raises(IndexError):
        StandingRule.broken(comms, "lead", observed=states, frame=frame)


def test_commander_page_reads_each_beats_frame_and_nothing_between(notification_bus):
    comms = notification_bus
    states = state_cards(comms, UnitState)
    reading = ObserveCommanderHud().observation()
    expected = beat(comms, states=states)
    client, observer = Pipe()
    with client, observer:
        service = ObservationService(comms, observer)
        try:
            reading.step(service, comms.views.revision())
            assert client.poll()
            assert client.recv().huds == (expected,)
            reading.step(service, comms.views.revision())
            assert not client.poll()
            beat(comms, states=states)
            reading.step(service, comms.views.revision())
            assert client.poll()
            assert isinstance(client.recv(), CommanderHudObserved)
        finally:
            reading.close()
            service.retire()


def test_a_frame_this_reader_cannot_decode_reaches_the_page_not_the_observer(notification_bus):
    """An installed newer heartbeat writes a field an older Toad does not declare; the page
    shows the unreadable frame and the observer process, and Toad, keep running."""
    comms = notification_bus
    reading = ObserveCommanderHud().observation()
    beat(comms, states=state_cards(comms, UnitState))
    frame, = CommanderFrame.matching_in(comms.root)
    written = json.loads(frame.read_text())
    frame.write_text(json.dumps({**written, "field_from_a_newer_build": []}))
    unreadable, = reading.read(comms, comms.views.revision())
    # The result crosses the observation process boundary by pickle.
    unreadable = pickle.loads(pickle.dumps(unreadable))
    assert isinstance(unreadable, observation.CommanderHudUnreadable)
    assert "field_from_a_newer_build" in str(unreadable.error)
    beat(comms, states=state_cards(comms, UnitState))
    recovered, = reading.read(comms, comms.views.revision())
    assert pickle.loads(pickle.dumps(recovered)).huds == CommanderFrame.read_all(comms.root)


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
    refused = CodexUsage(UnreadableUsage(reason='Codex: account unavailable'))
    assert refused.line() == 'usage unreadable: Codex: account unavailable'
    assert refused.needs_hand()
    assert FieldCodec.decode(CodexUsage, FieldCodec.encode(refused)) == refused


def test_waiting_notification_keeps_actionable_subjects_overflow_and_original_evidence(notification_bus):
    comms = notification_bus
    from agent_comms.head_review import PullRequestHead
    from agent_comms.repository_name import RepositoryName
    from agent_comms.standing_rules import OrderlessUnreviewedHead

    rules = tuple(OrderlessUnreviewedHead(
        request=PullRequestHead(RepositoryName.from_wire("OpenHCSDev/toad"), 100 + index, "a" * 40),
        due_at=1.0,
    ) for index in range(12))
    beat(comms, rules)
    original, = notifications(comms)
    packet = CommanderDecisions(
        original.task.commander, original.task.handled, (), original.task.handled,
    )
    from agent_comms.task_attachments.commander_pings import AlarmOverflow

    hud = replace(lead_hud(comms), packets=(packet,), open_pings=packet.handled)
    message = packet.send(comms, time.time(), hud.hud_text(time.time()))
    for frame in (lead_hud(comms), hud):
        assert len(frame.alarm_text(frame.alarms, time.time())) < len(rules)
        overflow = tuple(row for section in frame.alarms for table in section.tables(time.time())
                         if table.record is AlarmOverflow for row in table.records)
        assert sum(row.more for row in overflow) == len(rules) - 1
        now = time.time()
        original_lines = frame.alarm_detail(frame.alarms, now)
        assert all(section.heading() not in original_lines for section in frame.alarms)
        assert all(rule.line() in "\n".join(original_lines) for rule in rules)
    assert rules[0].line() in original.body
    assert rules[0].line() in message.body
    assert len(message.task.handled) == 12
    assert {entry.text for entry in message.task.board(comms, "lead")} == {rule.line() for rule in rules}
    assert len(pickle.loads(pickle.dumps(message.task)).handled) == 12
    from agent_comms.text_digest import TextDigest

    swapped = (*packet.handled[:-1], replace(packet.handled[-1], observation=TextDigest.of("other observation")))
    replacement = replace(packet, handled=swapped, waiting=swapped)
    changed = replace(hud, packets=(replacement,), open_pings=swapped)
    assert hud.alarm_text(hud.alarms, 1000) == changed.alarm_text(changed.alarms, 1000)
    assert hud.display_facts != changed.display_facts


def test_failed_quiet_publication_retries_identical_display_until_committed(notification_bus):
    """A real SQLite refusal rolls back publication, not the disposable frame write."""
    from agent_comms.task_attachments.commander_pings import PublishedFacts

    comms = notification_bus
    first_hud = beat(comms)
    first, = notifications(comms)
    assert isinstance(first.task, CommanderNotice)
    assert first.task.publication == PublishedFacts.of(first_hud.display_facts)
    comms.registry.register_child("quiet-fork", "lead", CommanderForkExecution, comms.goals)
    bus = comms.bus.bus
    with bus.writing() as db:
        db.execute(
            f'CREATE TRIGGER refuse_quiet BEFORE INSERT ON "{Message.table.declared_name}" '
            "BEGIN SELECT RAISE(ABORT, 'quiet publication refused'); END"
        )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="quiet publication refused"):
            beat(comms)
    finally:
        with bus.writing() as db:
            db.execute("DROP TRIGGER refuse_quiet")
    owner = comms.registry.require("lead").incarnation
    unsent = CommanderFrame.at(comms.root, owner).read()
    assert first_hud.display_facts != unsent.display_facts
    assert CommanderUpdate.latest(comms, owner) == (first.task,)
    assert len(notifications(comms)) == 1

    retried = beat(comms)
    assert retried.display_facts == unsent.display_facts
    _, committed = notifications(comms)
    assert committed.task.publication == PublishedFacts.of(retried.display_facts)
    assert committed.reference != first.reference
    beat(comms)
    assert tuple(message.reference for message in notifications(comms)) == (
        first.reference, committed.reference,
    )


def test_subject_evidence_is_shared_without_losing_each_pings_start(notification_bus, request):
    from agent_comms.heartbeat_subjects import CommanderHistory, HeartbeatSubject, UnitSubject
    from agent_comms.task_attachments.commander_pings import CommanderDelivery
    from test_holder_standing_rules import publish

    queries = []

    class CountedUnitSubject(UnitSubject):
        def action_at(self, history):
            queries.append(self)
            return super().action_at(history)  # Real addressed-message evidence, not a policy fake.

    request.addfinalizer(lambda: HeartbeatSubject.__registry__.pop(CountedUnitSubject.declared_name))
    comms = notification_bus
    owner = comms.registry.require("lead").incarnation
    worker = comms.registry.require("worker").incarnation
    now = time.time()
    action = publish(comms, "lead", "worker", now - 300)
    subject = CountedUnitSubject(worker)
    todo = comms.goals.todos.get("P1")
    rule = ReviewTimerUp(problem=todo.id, holder=worker, order=todo.order.checked().id,
                         since=RecordedEventTime(now - 600))
    old = replace(RulePing.capture(rule), subjects=(subject,), since=RecordedEventTime(now - 600))
    new = replace(old, since=RecordedEventTime(now - 200))
    packet = CommanderDecisions(owner, (old, new), (), ())
    history = CommanderHistory.read(comms, frozenset({"lead"}))
    assert history.action_at(subject) == RecordedEventTime(action.timestamp)
    assert old.acted(history) and not new.acted(history)
    opened = packet.open_pings(history)
    assert opened == (new,) and opened[0].since == new.since
    waiting = packet.undisposed(history, now)
    assert tuple(ping.since for ping in waiting) == (new.since,)
    hud = CommanderHud.of(comms, "lead", capture_frame(comms, "lead", ProgressRead()), (),
                          delivery=CommanderDelivery(packet, opened), usage=CodexUsage.read())
    assert hud.open_pings is opened
    assert hud.board == (new.board_entry(),)
    assert queries == [subject]
    # A later original action changes a fresh beat, never the already captured evidence.
    publish(comms, "lead", "worker", now)
    assert not new.acted(history)
    assert new.acted(CommanderHistory.read(comms, frozenset({"lead"})))
    assert queries == [subject, subject]


def test_retained_packet_without_display_witness_keeps_original_message_identity(notification_bus):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.task_attachments.commander_pings import PublishedFacts, UnpublishedFacts

    comms = notification_bus
    hud = beat(comms)
    message, = notifications(comms)
    encoded = FieldCodec.encode(message)
    # The original retained packet has no evidence for display facts; the default says so.
    del encoded["task"]["publication"]
    retained = FieldCodec.decode(Message, encoded)
    assert retained.message_id == message.message_id
    assert retained.reference == message.reference
    assert retained.body == message.body
    assert retained.task.publication == UnpublishedFacts()
    facts = PublishedFacts.of(hud.display_facts)
    assert retained.task.should_publish(facts, (retained.task,))
    assert not message.task.should_publish(facts, (message.task,))
    assert FieldCodec.decode(Message, FieldCodec.encode(retained)).message_id == message.message_id


def test_a_commander_that_stood_down_keeps_its_requests_but_gets_no_heartbeat(notification_bus):
    """The registry names the commander; an old open request does not make one."""
    comms = notification_bus
    lead = comms.registry.require("lead")
    assert lead.name in {thread.name for thread in CommanderHeartbeat.commanders(comms.registry, comms.goals)}
    assert CommanderHeartbeat.requested_by(lead, comms.goals)
    comms.registry.set_commander_project(RegisteredCommanderProject(
        comms.registry.require("worker").incarnation, ()))
    assert CommanderHeartbeat.requested_by(comms.registry.require("lead"), comms.goals)
    assert "lead" not in {
        thread.name for thread in CommanderHeartbeat.commanders(comms.registry, comms.goals)}
