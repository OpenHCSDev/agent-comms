"""Committed notifications survive a new watcher and use the real publisher/readers."""

import asyncio
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
from agent_comms.cli_verbs.fork import ForkCliCommand
from agent_comms.thread_management import GivenTags
from agent_comms.commander_heartbeat import CommanderHeartbeat, CommanderObservation
from agent_comms.comms import Comms
from commander_heartbeat_fixture import ObservedHeartbeat, capture_frame
from agent_comms.commander_hud import (
    BoardRow, CommanderFrame, CommanderHud, CommanderUnit, CodexUsage, QuietUnits, UnreadableUsage,
)
from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.task_attachments.commander_pings import CommanderDecisions, CommanderNotice, CommanderUpdate, RulePing
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.launch_root import LaunchRoot
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import DMDisplayScope
from agent_comms.service_passes import ServicePassStore
from agent_comms.todos import ProgressRead, UnitOccupied
from agent_comms.ui_model import observation
from agent_comms.ui_model.observation import CommanderHudObserved, ObservationService, ObserveCommanderHud
from agent_comms.ui_model.observation import ReadCommanderUnit, ReadRefused
from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.standing_rules import ServicePassFailing, StandingRule, UndatedStandingRule
from agent_comms.ui_model.unit_state import (
    AwaitingApplicationRun, NoPendingClock, PendingClock, Spinning, Stuck, UnitState,
)
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms_hooks.board import BoardEntry
from agent_comms_hooks.watch import RecordedWatch, WatchClaim, WatchRecord
from agent_comms.wake_policy import PassiveWake
from agent_comms.bus_publication import AgentOrigin, HumanOrigin
from agent_comms.channel_targets import CommandersTag, ThreadTarget
from agent_comms.messages import ConversationRole, InfoKind, Message
from agent_comms.transcript_events import IncomingTranscript
from agent_comms.ui_model.message_category import HeartbeatCategory, InboundCategory, all_categories
from agent_comms.repo import Checkout
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


def owner(comms):
    """The owner's thread, which the heartbeat speaks under."""
    return comms.messaging.user_identity(Checkout(comms.root))


def notifications(comms):
    page = MessagePageRequest(DMDisplayScope.capture(
        owner(comms).name, "lead", comms.registry.snapshot(),
    )).read_opened(comms.bus.bus)
    return tuple(message for message in page.messages if isinstance(message.task, CommanderUpdate))


def lead_hud(comms, states=()):
    return CommanderHud.of(comms, "lead", capture_frame(comms, "lead", ProgressRead()), states)


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
    return ServicePassFailing(watch=BrokenHeartbeat.__name__, since=stored.since, error=stored.error)


def aged(pings):
    return tuple((ping, ping.since) for ping in pings)


def test_failed_heartbeat_pass_publishes_one_typed_packet_and_still_raises(notification_bus):
    comms = notification_bus
    raised = failed_pass(comms, RuntimeError("first wording"))
    assert str(raised) == "forced heartbeat pass failure"
    failure = retained_failure(comms)
    message, = notifications(comms)
    assert message.sender == owner(comms).name and message.target == "lead"
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
             ServicePassFailing(watch="independent", since=1.0, error="another source"))
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
    """A taskless message the owner typed, under the same thread the heartbeat speaks in."""
    return comms.messaging.send_user_message(ThreadTarget("lead"), body, worktree=Checkout(comms.root))


def everything_to_lead(comms):
    page = MessagePageRequest(DMDisplayScope.capture(
        owner(comms).name, "lead", comms.registry.snapshot(),
    )).read_opened(comms.bus.bus)
    return {message.reference for message in page.messages}


def test_an_independent_heartbeat_message_never_replaces_the_board(notification_bus):
    comms = notification_bus
    failing = ServicePassFailing(watch="following-rule", since=time.time(), error="independent source")
    beat(comms, (failing,))
    board, = notifications(comms)
    raw = independent_instruction(comms, "Heartbeat pass failed: an old raw alarm")
    lead = comms.registry.require("lead").incarnation
    assert CommanderUpdate.latest(comms, lead) == (board.task,)
    assert CommanderUpdate.current_board(comms, "lead") == board.task.board(comms)
    assert lead_hud(comms).board == board.task.board(comms)
    assert raw.reference in everything_to_lead(comms)
    # The next pass derives from the typed board, so nothing is restated as new.
    beat(comms, (failing,))
    assert len(notifications(comms)) == 1


def test_a_later_notice_is_the_baseline_and_keeps_the_lines_still_on_it(notification_bus):
    comms = notification_bus
    kept = ServicePassFailing(watch="kept-rule", since=time.time(), error="still failing")
    cleared = ServicePassFailing(watch="cleared-rule", since=time.time(), error="now fixed")
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
    rules = (ServicePassFailing(watch="independent", since=1.0, error="another source"),)
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
    failing = ServicePassFailing(watch="following-rule", since=time.time(), error="independent source")
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
    user = owner(comms)
    for sender, origin, commander in (("worker", AgentOrigin(), lead),
                                      (user.name, HumanOrigin.of(user), worker)):
        with pytest.raises(RelationViolationError):
            comms.bus.publisher.publish_ordinary(
                Message(sender=sender, target="lead", body="forged board", type=InfoKind,
                        role=CommanderNotice.role, task=CommanderNotice(commander, ())),
                origin=origin, destination=ThreadTarget("lead"),
            )
    assert CommanderUpdate.latest(comms, lead) == ()
    assert CommanderUpdate.latest(comms, worker) == ()


def test_failure_packet_follows_the_commander_rename_alias(notification_bus):
    comms = notification_bus
    failed_pass(comms, RuntimeError("down"))
    first, = notifications(comms)
    comms.threads._rename_thread("lead", "renamed-lead")
    failed_pass(comms, RuntimeError("still down"))
    page = MessagePageRequest(DMDisplayScope.capture(
        owner(comms).name, "renamed-lead", comms.registry.snapshot(),
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
    hud = CommanderHud(commander=lead, units=(), phases=(
        CommanderPhase(RetainedStorePhase, True, False, ()),
        CommanderPhase(OneOrderOwnerPhase, True, False, ()),
        CommanderPhase(DesignReconciledPhase, False, False, (worker,)),
        CommanderPhase(RecordPlanePhase, False, True, ()),
        CommanderPhase(ReviewQueuePhase, True, False, (worker,)),
    ), load=OverloadDegradation.from_orders((), 0, 0), forks=(worker,),
        usage=CodexUsage(UnreadableUsage(reason='not requested')))
    assert hud.frame == (
        *hud.header,
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
    board = (
        BoardEntry("blocked", "BLOCKED waiting-run", now - 120, now + 3600),
        BoardEntry("judge", "JUDGE #1", now - 240, now + 3600),
    )
    hud = CommanderHud(
        commander=RecordedThreadIncarnation("lead", 3.0), units=(awaiting, stuck), phases=(),
        load=OverloadDegradation.from_orders((), 0, 0), forks=(),
        usage=CodexUsage(UnreadableUsage(reason="not requested")), board=board,
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
    failing = ServicePassFailing(watch="following-rule", since=time.time(), error="independent source")
    frame = capture_frame(comms, "lead", ProgressRead())
    frames = tuple(CommanderObservation(thread, frame, (failing,), ())
                   for thread in (lead, missing))
    with pytest.raises(UnregisteredThreadError):
        asyncio.run(ObservedHeartbeat(frames).run(lambda: nullcontext(comms)))
    completed = ServicePassStore.at(comms.root).read().commander_completed
    assert set(completed) == {lead.incarnation}
    assert missing.incarnation not in completed
    # A quiet pass, its line unchanged, still completes after its original notice was committed.
    beat(comms, (failing,))
    assert len(notifications(comms)) == 1
    # A pass that misses a still-registered commander retires nothing but its frame.
    asyncio.run(ObservedHeartbeat(()).run(lambda: nullcontext(comms)))
    assert CommanderFrame.read_all(comms.root) == ()
    assert set(ServicePassStore.at(comms.root).read().commander_completed) == {lead.incarnation}
    # Only the registry retiring the incarnation drops its completion.
    comms.registry.unregister("lead")
    comms.registry.remove("lead")
    asyncio.run(ObservedHeartbeat(()).run(lambda: nullcontext(comms)))
    assert not ServicePassStore.at(comms.root).read().commander_completed


def test_recreated_watcher_reads_original_committed_unit_observation(notification_bus):
    """Quiet unit cards are not heartbeat lines; a fresh watcher reads the committed packet
    and restates nothing it already carries."""
    comms = notification_bus
    states = state_cards(comms, UnitState)
    assert states and not any(state.attention() for state in states)
    failing = ServicePassFailing(watch="following-rule", since=time.time(), error="independent source")
    beat(comms, (failing,), states=states)
    first, = notifications(comms)
    assert failing.line() in first.body
    assert not any(state.line() in first.body for state in states)
    hud = lead_hud(comms, states)
    assert first.body.startswith(CommanderUpdate.header)
    assert all(line in first.body for line in hud.brief())
    assert all(card.unit.name in first.body for card in hud.units)
    assert CommanderUpdate.latest(comms, comms.registry.require("lead").incarnation) == (first.task,)
    beat(comms, (failing,), states=state_cards(comms, UnitState))
    unchanged, = notifications(comms)
    assert unchanged.reference == first.reference


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


def test_rule_packets_list_every_new_line_and_never_restate_one(notification_bus):
    comms = notification_bus
    rules = tuple(ServicePassFailing(watch=f"watch-{index}", since=1.0, error="failed source read")
                  for index in range(7))
    beat(comms, rules)
    first, = notifications(comms)
    assert isinstance(first.task, CommanderDecisions)
    assert set(first.task.decisions) == set(first.task.handled) == {
        RulePing.capture(rule) for rule in rules}
    assert all(rule.line() in first.body for rule in rules)
    assert all(line in first.body for line in lead_hud(comms).brief())
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
    assert all(line in quiet.body for line in lead_hud(comms).brief())
    assert all(isinstance(decision.wake_mode, PassiveWake) for decision in quiet.decisions)
    # Nothing new and nothing cleared sends nothing.
    beat(comms, ServicePassFailing.violations(comms, "lead"))
    assert len(notifications(comms)) == 2
    # Use the same source occurrence to prove it was retired, not merely hidden.
    beat(comms, rules)
    _, _, repeated = notifications(comms)
    assert repeated.task.decisions == first.task.decisions


def test_latest_observation_follows_the_commander_rename_alias(notification_bus):
    comms = notification_bus
    beat(comms, (ServicePassFailing(watch="watch", since=1.0, error="failed source read"),))
    first, = notifications(comms)
    comms.threads._rename_thread("lead", "renamed-lead")
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
    assert len(notifications(comms)) == 1


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


def test_waiting_notification_is_bounded_but_original_packet_keeps_every_alert(notification_bus):
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
    message = packet.send(comms, time.time(), lead_hud(comms).brief())
    assert sum(f"#{rule.request.pr} " in message.body for rule in rules) == 2
    assert max(map(len, message.body.splitlines())) < 300
    assert len(message.task.handled) == 12
    assert {entry.text for entry in message.task.board(comms)} == {rule.line() for rule in rules}
    assert len(pickle.loads(pickle.dumps(message.task)).handled) == 12


def test_a_commander_that_stood_down_keeps_its_requests_but_gets_no_heartbeat(notification_bus):
    """The registry names the commander; an old open request does not make one."""
    comms = notification_bus
    lead = comms.registry.require("lead")
    assert lead.name in {thread.name for thread in CommanderHeartbeat.boards(comms.registry, comms.goals)}
    assert CommanderHeartbeat.requested_by(lead, comms.goals)
    comms.registry.set_commander_project(RegisteredCommanderProject(
        comms.registry.require("worker").incarnation, ()))
    assert CommanderHeartbeat.requested_by(comms.registry.require("lead"), comms.goals)
    assert "lead" not in {
        thread.name for thread in CommanderHeartbeat.boards(comms.registry, comms.goals)}


def test_one_pass_gives_every_commander_its_own_packet(notification_bus):
    """#commanders declares who commands: one board read, one packet per live member under its
    own incarnation. A thread outside the channel, and a fork of a commander (in the channel
    with its commander's tags, acting for it), get none."""
    comms = notification_bus
    live = ProcessIdentity.capture(os.getpid())
    for name, tags in (("worker", {"comms", CommandersTag.declared_name}), ("outsider", {"comms"})):
        comms.registry.register(replace(comms.registry.require(name), tags=frozenset(tags),
                                        process_identity=live))
    fork = comms.registry.register_child("worker-fork", "worker", CommanderForkExecution, comms.goals)
    comms.registry.register(replace(fork, process_identity=live))
    assert CommandersTag.declared_name in comms.registry.require("worker-fork").tags
    failing = ServicePassFailing("following-rule", time.time(), "independent source")
    beat(comms, (failing,))
    lead_packet, = CommanderUpdate.latest(comms, comms.registry.require("lead").incarnation)
    worker = comms.registry.require("worker").incarnation
    worker_packet, = CommanderUpdate.latest(comms, worker)
    assert worker_packet.commander == worker
    assert worker_packet.handled == lead_packet.handled == (RulePing.capture(failing),)
    assert CommanderUpdate.latest(comms, comms.registry.require("outsider").incarnation) == ()
    assert CommanderUpdate.latest(comms, comms.registry.require("worker-fork").incarnation) == ()
    assert set(ServicePassStore.at(comms.root).read().commander_completed) == {
        comms.registry.require("lead").incarnation, worker}


def test_fork_cli_default_inherits_audience_but_not_commander_role(notification_bus):
    comms = notification_bus
    parent = comms.registry.require('worker')
    tag = CommandersTag.declared_name
    parent = comms.registry.register(replace(parent, tags=parent.tags | {'comms', tag}))
    command = ForkCliCommand.thread_binding(parent)
    assert command.tags.for_new() == parent.tags - {tag}
    explicit = replace(command, tags=GivenTags(parent.tags))
    assert tag in explicit.tags.for_new()


def test_a_commander_is_declared_never_inherited(notification_bus, tmp_path):
    """The hook's role answer reads the one derivation: a thread registered with the tag
    commands; a fork carries its commander's tags but acts for it, and a bound unit of a
    commander does not inherit the role."""
    comms = notification_bus
    tag = CommandersTag.declared_name
    for name in ("lead", "worker"):
        thread = comms.registry.require(name)
        comms.registry.register(replace(thread, tags=thread.tags | {tag}))
    declared = comms.registry.register_child("declared-fork", "worker", CommanderForkExecution, comms.goals)
    comms.registry.register(replace(declared, tags=declared.tags | {tag}))
    comms.registry.register_child("copied-fork", "worker", CommanderForkExecution, comms.goals)
    worker = comms.registry.require("worker")
    comms.registry.register_commanded_unit(
        "bound-unit", "worker", worker.worktree, ProcessIdentity.capture(os.getpid()))

    def commands(name):
        return RecordedWatch(WatchClaim(name, home=tmp_path),
                             WatchRecord(name, str(comms.root), (), tmp_path)).commands()

    assert commands("lead") and commands("worker")
    assert not commands("outsider")
    assert not commands("declared-fork") and not commands("copied-fork")
    assert not commands("bound-unit")
    assert {"comms", tag} <= comms.registry.require("copied-fork").tags


def test_a_board_packet_files_under_heartbeats_while_the_owners_own_words_stay_inbound(notification_bus):
    comms = notification_bus
    lead = comms.registry.require("lead").incarnation
    user = owner(comms)
    board = Message(sender=user.name, target="lead", body="board", type=InfoKind,
                    role=CommanderDecisions.role, task=CommanderDecisions(lead, (), (), ()))
    spoken = Message(sender=user.name, target="lead", body="his words", type=InfoKind,
                     role=ConversationRole())
    assert board.category is HeartbeatCategory
    assert spoken.category is InboundCategory
    assert IncomingTranscript.of(board, reader="lead").category is HeartbeatCategory
    assert HeartbeatCategory in all_categories()
