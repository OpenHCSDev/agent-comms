"""Committed notifications survive a new watcher and use the real publisher/readers."""

import json
import os
import pickle
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from multiprocessing import Pipe

import pytest

from agent_comms.registry_document import RegisteredCommanderProject

from agent_comms.child_process import ProcessIdentity
from agent_comms.commander_heartbeat import CommanderHeartbeat, CommanderObservation
from agent_comms.commander_hud import (
    BoardRow, CommanderFrame, CommanderHud, CommanderUnit, CodexUsage, QuietUnits, UnreadableUsage,
)
from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.task_attachments.commander_pings import CommanderDecisions, CommanderNotice, CommanderUpdate, RulePing
from agent_comms.errors import OverdueHeadRefusalError, RelationViolationError, UnregisteredThreadError
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.launch_root import LaunchRoot
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import DMDisplayScope
from agent_comms.service_passes import ServicePassStore
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import ProgressRead
from agent_comms.ui_model import observation
from agent_comms.ui_model.observation import CommanderHudObserved, ObservationService, ObserveCommanderHud
from agent_comms.ui_model.observation import ReadCommanderUnit, ReadRefused
from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.standing_rules import ServicePassFailing, StandingRule, UndatedStandingRule
from agent_comms.ui_model.unit_state import (
    AwaitingApplicationRun, NoPendingClock, PendingClock, Spinning, Stuck, UnitState,
)
from agent_comms.thread_identity import RecordedThreadIncarnation
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


def notifications(comms):
    page = MessagePageRequest(DMDisplayScope.capture(
        CommanderHeartbeat.sender, "lead", comms.registry.snapshot(),
    )).read_opened(comms.bus.bus)
    return tuple(message for message in page.messages if isinstance(message.task, CommanderUpdate))


def lead_hud(comms, states=()):
    return CommanderHud.of(comms, "lead", HeartbeatFrame.capture(comms, "lead", ProgressRead()), states)


def beat(comms, rules=(), *, states=()):
    frame = CommanderObservation(
        comms.registry.require("lead"), (), rules, states, hud=lead_hud(comms, states))
    # Deliberately construct a fresh watcher on every pass, never reuse a cache.
    CommanderHeartbeat().pass_body(comms, (frame,))
    return next(hud for hud in CommanderFrame.read_all(comms.root)
                if hud.commander == frame.hud.commander)


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
    def observe(self, comms):
        raise RuntimeError("forced heartbeat pass failure")


def failed_pass(comms, error):
    """One failed pass in the coordinator's order: the store records it, then a fresh
    watcher's callback runs; the original exception still escapes the pass."""
    heartbeat = BrokenHeartbeat()
    with pytest.raises(RuntimeError) as raised:
        heartbeat.watch_pass(lambda: nullcontext(comms))
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
            raise OverdueHeadRefusalError("Review overdue heads before sending new orders:\ntoad#1176")

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
    assert isinstance(recovered.task, CommanderNotice)
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


def test_an_independent_heartbeat_message_never_replaces_the_board(notification_bus):
    comms = notification_bus
    failing = ServicePassFailing("following-rule", time.time(), "independent source")
    beat(comms, (failing,))
    board, = notifications(comms)
    raw = independent_instruction(comms, "Heartbeat pass failed: an old raw alarm")
    lead = comms.registry.require("lead").incarnation
    assert CommanderUpdate.latest(comms, lead) == (board.task,)
    assert CommanderUpdate.current_board(comms, "lead") == board.task.board(comms, "lead")
    assert tuple(entry.text for entry in lead_hud(comms).board) == tuple(
        ping.text for ping in board.task.handled)

    assert raw.reference in everything_to_lead(comms)
    # The next pass derives from the typed board and passively repeats the open alarm.
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
    ), OverloadDegradation.from_orders((), 0, 0), (worker,), CodexUsage(UnreadableUsage(reason='not requested')))
    assert (str(hud.load), *(row.line() for row in hud.rows())) == (
        str(hud.load),
        "commander_native/record_plane done=False ready=True holders=",
        "commander_native/review_queue done=True ready=False holders=worker",
        "commander_native/design_reconciled done=False ready=False holders=worker",
        "usage unreadable: not requested",
        "forks: worker",
        "commander_native: 2 phases done",
    )


def test_frame_keeps_committed_board_order_and_folds_units_needing_no_hand(tmp_path):
    now = time.time()
    awaiting = CommanderUnit(
        RecordedThreadIncarnation("waiting-run", 1.0), AwaitingApplicationRun,
        NoPendingClock(), (), 0, (), (),
    )
    stuck = replace(
        awaiting, unit=RecordedThreadIncarnation("stuck-worker", 2.0),
        state=Stuck, clock=PendingClock(now - 3600, now - 3600),
    )
    pings = tuple(replace(RulePing.capture(ServicePassFailing(name, began, text)),
                          since=RecordedEventTime(began))
                  for name, began, text in (("blocked", now - 120, "BLOCKED waiting-run"),
                                            ("judge", now - 240, "JUDGE #1")))
    board = tuple(ping.board_entry() for ping in pings)
    hud = CommanderHud(
        RecordedThreadIncarnation("lead", 3.0), (awaiting, stuck), (),
        OverloadDegradation.from_orders((), 0, 0), (),
        CodexUsage(UnreadableUsage(reason="not requested")),
        (CommanderNotice(RecordedThreadIncarnation("lead", 3.0), pings),),
    )

    CommanderFrame.replace_all(tmp_path, (hud,))
    restored, = CommanderFrame.read_all(tmp_path)
    assert restored == hud
    rows = restored.rows()
    assert rows[:4] == (
        BoardRow(board[0]), BoardRow(board[1]), stuck, QuietUnits((awaiting,)),
    )
    assert awaiting.unit.name in rows[3].line()
    assert all("AwaitingApplicationRun" not in line for line in restored.frame)


def test_a_registered_commander_fork_is_on_the_hud(notification_bus):
    comms = notification_bus
    fork = comms.registry.register_child("judge-fork", "lead", CommanderForkExecution, comms.goals)
    assert fork.incarnation in lead_hud(comms).forks


def test_quiet_completion_is_per_commander_even_when_a_later_recipient_fails(notification_bus):
    comms = notification_bus
    lead = comms.registry.require("lead")
    missing = replace(comms.registry.require("worker"), name="unregistered-recipient")
    failing = ServicePassFailing("following-rule", time.time(), "independent source")
    frames = tuple(CommanderObservation(thread, (), (failing,), (), hud=lead_hud(comms))
                   for thread in (lead, missing))
    with pytest.raises(UnregisteredThreadError):
        CommanderHeartbeat().pass_body(comms, frames)
    completed = ServicePassStore.at(comms.root).read().commander_completed
    assert set(completed) == {lead.incarnation}
    assert missing.incarnation not in completed
    # A quiet pass still completes and keeps the original alarm visibly open.
    beat(comms, (failing,))
    assert len(notifications(comms)) == 2
    # A pass that misses a still-registered commander retires nothing but its frame.
    CommanderHeartbeat().pass_body(comms, ())
    assert CommanderFrame.read_all(comms.root) == ()
    assert set(ServicePassStore.at(comms.root).read().commander_completed) == {lead.incarnation}
    # Only the registry retiring the incarnation drops its completion.
    comms.registry.unregister("lead")
    comms.registry.remove("lead")
    CommanderHeartbeat().pass_body(comms, ())
    assert not ServicePassStore.at(comms.root).read().commander_completed


def test_recreated_watcher_keeps_original_alarm_visible_without_a_new_wake(notification_bus):
    """A fresh watcher retains the original alarm age and repeats it as a passive notice."""
    comms = notification_bus
    states = state_cards(comms, UnitState)
    assert states and not any(state.attention() for state in states)
    failing = ServicePassFailing("following-rule", time.time(), "independent source")
    beat(comms, (failing,), states=states)
    first, = notifications(comms)
    assert failing.line() in first.body
    assert not any(state.line() in first.body for state in states)
    hud = lead_hud(comms, states)
    assert first.body.startswith("LOAD ")
    assert f"UNJUDGED {hud.load.unjudged}" in first.body
    assert all(card.unit.name in first.body for card in hud.units)
    assert CommanderUpdate.latest(comms, comms.registry.require("lead").incarnation) == (first.task,)
    beat(comms, (failing,), states=state_cards(comms, UnitState))
    _, unchanged = notifications(comms)
    assert unchanged.reference != first.reference
    assert isinstance(unchanged.task, CommanderNotice)
    assert aged(unchanged.task.handled) == aged(first.task.handled)
    assert failing.line() in unchanged.body.split("STILL OPEN", 1)[1]


def test_overdue_head_refusal_is_published_and_later_rule_still_runs(notification_bus):
    comms = notification_bus
    enforced = []

    class FollowingRule(ServicePassFailing):
        def enforce(self, _comms, _commander):
            enforced.append(self.watch)

    class OverdueRule(FollowingRule):
        def enforce(self, _comms, _commander):
            super().enforce(_comms, _commander)
            raise OverdueHeadRefusalError("Review overdue heads before sending new orders:\ntoad#1176")

    rules = (OverdueRule("overdue-head", 1.0, "review pending"),
             FollowingRule("following-rule", 1.0, "another rule"))
    beat(comms, rules)

    assert enforced == ["overdue-head", "following-rule"]
    message, = notifications(comms)
    assert "Not applied (a reviewed head is overdue):" in message.body
    assert rules[0].line() in message.body
    assert RulePing.capture(rules[0]) not in message.task.handled
    assert RulePing.capture(rules[1]) in message.task.handled
    assert comms.registry.require("lead").incarnation in (
        ServicePassStore.at(comms.root).read().commander_completed)


def test_rule_packets_list_every_new_line_then_repeat_it_without_a_new_wake(notification_bus):
    comms = notification_bus
    rules = tuple(ServicePassFailing(f"watch-{index}", 1.0, "failed source read")
                  for index in range(7))
    beat(comms, rules)
    first, = notifications(comms)
    assert isinstance(first.task, CommanderDecisions)
    assert set(first.task.decisions) == set(first.task.handled) == {
        RulePing.capture(rule) for rule in rules}
    assert all(rule.line() in first.body for rule in rules)
    assert first.body.startswith("LOAD ")
    beat(comms, rules)
    _, unchanged = notifications(comms)
    assert isinstance(unchanged.task, CommanderNotice)
    assert all(rule.line() in unchanged.body.split("STILL OPEN", 1)[1] for rule in rules)
    # Changed diagnostic wording is visible but not another failed occurrence.
    beat(comms, tuple(replace(rule, error="new diagnostic detail") for rule in rules))
    _, _, changed = notifications(comms)
    assert isinstance(changed.task, CommanderNotice)
    assert aged(changed.task.handled) == aged(first.task.handled)
    assert "new diagnostic detail" in changed.body


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
    assert quiet.body.startswith("LOAD ")
    assert all(isinstance(decision.wake_mode, PassiveWake) for decision in quiet.decisions)
    # Even an empty board publishes the current HUD without waking its commander.
    beat(comms, ServicePassFailing.violations(comms, "lead"))
    assert len(notifications(comms)) == 3
    # Use the same source occurrence to prove it was retired, not merely hidden.
    beat(comms, rules)
    _, _, _, repeated = notifications(comms)
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
    assert isinstance(repeated.task, CommanderNotice)
    assert aged(repeated.task.handled) == aged(original.task.handled)


def test_failed_rule_scan_raises_to_the_heartbeat(notification_bus, request):
    comms = notification_bus
    frame = HeartbeatFrame.capture(comms, "lead", ProgressRead())
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


def test_waiting_notification_keeps_every_open_alarm_and_its_original_evidence(notification_bus):
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
    hud = replace(lead_hud(comms), packets=(packet,))
    message = packet.send(comms, time.time(), hud.hud_text(time.time()))
    assert sum(f"#{rule.request.pr} " in message.body for rule in rules) == len(rules)
    assert all(rule.line() in message.body.split("STILL OPEN", 1)[1] for rule in rules)
    assert len(message.task.handled) == 12
    assert {entry.text for entry in message.task.board(comms, "lead")} == {rule.line() for rule in rules}
    assert len(pickle.loads(pickle.dumps(message.task)).handled) == 12


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
