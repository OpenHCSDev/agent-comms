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
from agent_comms.errors import OverdueHeadRefusalError, UnregisteredThreadError
from agent_comms.heartbeat_frame import HeartbeatFrame
from agent_comms.launch_root import LaunchRoot
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import DMDisplayScope
from agent_comms.service_passes import ServicePassStore
from agent_comms.todos import ProgressRead
from agent_comms.ui_model import observation
from agent_comms.ui_model.observation import CommanderHudObserved, ObservationService, ObserveCommanderHud
from agent_comms.field_codec import FieldCodec
from agent_comms.order_tools import CommanderUnitRead
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import invoke_tool
from agent_comms.ui_model.observation import ReadOperation, ReadRefused
from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.standing_rules import ServicePassFailing, StandingRule, UndatedStandingRule
from agent_comms.ui_model.unit_state import (
    AwaitingApplicationRun, NoPendingClock, PendingClock, Stuck, UnitState,
)
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms_hooks.board import BoardEntry
from agent_comms.wake_policy import PassiveWake
from test_holder_standing_rules import state_cards
from test_unit_state import recorded_turn

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
    return frame.hud


def test_expanded_unit_reads_original_orders_and_bounded_activity_then_refuses_stale_selection(
    notification_bus,
):
    comms = notification_bus
    recorded_turn(comms, 10)
    original = (comms.root / 'state-source.jsonl').read_bytes()
    hud = beat(comms, states=state_cards(comms, UnitState))
    ServicePassStore.at(comms.root).heartbeat_passed((hud.commander,), (hud.commander,))
    card = next(card for card in hud.units if card.unit.name == 'worker')
    operation = CommanderUnitRead(commander=hud.commander, unit=card.unit,
                                  orders=tuple(dict.fromkeys(order.order for order in card.orders)))
    request = ReadOperation(str(comms.root), operation)
    direct = operation.read(comms)
    detail = pickle.loads(pickle.dumps(request.read(comms)))
    # The shared operation, the UI's root-bound envelope and the MCP boundary answer the same
    # selected card with the same bounded records.
    assert detail == direct
    assert invoke_tool(comms, CommanderUnitRead.declared_name, FieldCodec.encode(operation),
                       caller=ThreadCaller("lead")) == FieldCodec.encode(direct)
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
        replace(request, operation=replace(operation, orders=())),
        replace(request, root=str(comms.root / 'other')),
        replace(request, operation=replace(operation, unit=replace(card.unit, created_at=card.unit.wire_birth + 1))),
        replace(request, operation=replace(operation, commander=card.unit)),
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


def test_failed_heartbeat_pass_messages_commander_and_still_raises(notification_bus):
    failure = RuntimeError("forced heartbeat pass failure")

    class BrokenHeartbeat(CommanderHeartbeat, declared_name=None):
        def observe(self, comms):
            raise failure

    comms = notification_bus
    assert "lead" in {thread.name for thread in BrokenHeartbeat.commanders(comms.registry, comms.goals)}
    heartbeat = BrokenHeartbeat()
    with pytest.raises(RuntimeError) as raised:
        heartbeat.watch_pass(lambda: nullcontext(comms))
    assert raised.value is failure
    heartbeat.failed(lambda: nullcontext(comms), raised.value)
    page = MessagePageRequest(DMDisplayScope.capture(
        CommanderHeartbeat.sender, "lead", comms.registry.snapshot(),
    )).read_opened(comms.bus.bus)
    notices = tuple(message for message in page.messages if str(failure) in message.body)
    assert len(notices) == 1
    assert notices[0].sender == CommanderHeartbeat.sender
    assert notices[0].target == "lead"


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
    assert hud.frame == (
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
    board = (
        BoardEntry("blocked", "BLOCKED waiting-run", now - 120, now + 3600),
        BoardEntry("judge", "JUDGE #1", now - 240, now + 3600),
    )
    hud = CommanderHud(
        RecordedThreadIncarnation("lead", 3.0), (awaiting, stuck), (),
        OverloadDegradation.from_orders((), 0, 0), (),
        CodexUsage(UnreadableUsage(reason="not requested")), board,
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
    fork = comms.registry.register_commander_child("judge-fork", "lead", comms.goals)
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
    # A quiet pass, its line unchanged, still completes after its original notice was committed.
    beat(comms, (failing,))
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
    """Quiet unit cards are not heartbeat lines; a fresh watcher reads the committed packet
    and restates nothing it already carries."""
    comms = notification_bus
    states = state_cards(comms, UnitState)
    assert states and not any(state.attention() for state in states)
    failing = ServicePassFailing("following-rule", time.time(), "independent source")
    beat(comms, (failing,), states=states)
    first, = notifications(comms)
    assert failing.line() in first.body
    assert not any(state.line() in first.body for state in states)
    hud = lead_hud(comms, states)
    assert first.body.startswith(CommanderUpdate.header)
    assert all(line in first.body for line in hud.brief())
    assert all(card.unit.name in first.body for card in hud.units)
    assert CommanderUpdate.latest(comms, comms.registry.require("lead").incarnation) == first.task
    beat(comms, (failing,), states=state_cards(comms, UnitState))
    unchanged, = notifications(comms)
    assert unchanged.reference == first.reference


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


def test_rule_packets_list_every_new_line_and_never_restate_one(notification_bus):
    comms = notification_bus
    rules = tuple(ServicePassFailing(f"watch-{index}", 1.0, "failed source read")
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
    message = packet.send(comms, time.time(), lead_hud(comms))
    assert sum(f"#{rule.request.pr} " in message.body for rule in rules) == 2
    assert max(map(len, message.body.splitlines())) < 300
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
