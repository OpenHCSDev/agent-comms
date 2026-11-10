"""Committed notifications survive a new watcher and use the real publisher/readers."""

import asyncio
import json
import os
import pickle
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from multiprocessing import Pipe

import pytest

from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.cli_bridges.session_watch import WatchClaim
from agent_comms.cli_verbs.fork import ForkCliCommand
from agent_comms.comms import Comms
from agent_comms.coordination.errors import DeferredRead
from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.errors import (
    HumanSendRefusedError,
    RelationViolationError,
    UnregisteredThreadError,
)
from agent_comms.execution.child_process import NoProcess, ProcessIdentity
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.messaging.bus_publication import AgentOrigin
from agent_comms.messaging.bus_read import UiRead
from agent_comms.messaging.channel_targets import TeammatesTag, ThreadTarget
from agent_comms.messaging.initial_origin import HumanOrigin
from agent_comms.messaging.message_page import MessagePageRequest
from agent_comms.messaging.messages import ConversationRole, InfoKind, Message
from agent_comms.messaging.read_basis import DMDisplayScope
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning.orders import ProgressRead
from agent_comms.planning.todo_refusals import TeammateOccupied
from agent_comms.presentation import NoRevision
from agent_comms.projects.checkouts import Checkout, UnpushedHead
from agent_comms.projects.work_branches import WorkBranchError
from agent_comms.registry.heartbeat_delivery import OffHeartbeatDelivery, OnHeartbeatDelivery
from agent_comms.registry.thread_management import GivenTags
from agent_comms.registry.threads import ForkThread, NativeThread, Thread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.task_attachments.team_pings import (
    PublishedFacts,
    RulePing,
    TeamDecisions,
    TeamUpdate,
    TeamUpdateNotice,
    TeamUpdateStore,
)
from agent_comms.team.standing_rules import (
    ReviewTimerUp,
    ServicePassFailing,
    StandingRule,
    UndatedStandingRule,
)
from agent_comms.team.team_heartbeat import TeamHeartbeat, TeamObservation
from agent_comms.team.team_hud import CodexUsage, TeamFrame, TeamHud, TeamPlan, UnreadableUsage
from agent_comms.team.team_hud_records import Teammate
from agent_comms.team.vibe_check import OverloadDegradation
from agent_comms.transcript.transcript_events import IncomingTranscript
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.reads import TeammateRead
from agent_comms.transport.tools.served import invoke_tool
from agent_comms.ui_model import observation
from agent_comms.ui_model.hud_groups import PhasesDoneByPlanGroup
from agent_comms.ui_model.message_category import HeartbeatCategory, InboundCategory, all_categories
from agent_comms.ui_model.observation import (
    ObservationService,
    ObserveTeamHud,
    ReadOperation,
    ReadRefused,
    TeamHudObserved,
)
from agent_comms.ui_model.teammate_state import Spinning, TeammateState, UrgentGroup
from agent_comms.waking.wake_policy import PassiveWake
from agent_comms_hooks.watch import WatchRecord
from team_heartbeat_fixture import ObservedHeartbeat, capture_frame, current_hud
from test_holder_standing_rules import state_cards
from test_unit_state import recorded_turn

pytest_plugins = ("test_holder_standing_rules",)


@pytest.fixture
def notification_bus(held_bus, installed_runtime, monkeypatch, tmp_path):
    # This private bus has no account server. Exercise the actual unavailable
    # endpoint rather than querying the operator's live account on every beat.
    monkeypatch.setattr(CodexConnection, "CONTROL_SOCKET", tmp_path / "no-account.sock")
    colleague = held_bus.registry.require("colleague")
    assert colleague.heartbeat_delivery is OffHeartbeatDelivery
    # These tests are about publication, which a thread turns on: the default is off.
    held_bus.registry.register(replace(
        colleague, heartbeat_delivery=OnHeartbeatDelivery,
        process_identity=ProcessIdentity.capture(os.getpid())))
    for teammate in ("worker", "outsider"):
        held_bus.threads.set_heartbeat_delivery(teammate, OnHeartbeatDelivery)
    with LaunchRoot.from_path(held_bus.root).selected():
        yield held_bus


@pytest.mark.parametrize(("delivery", "publications"), (("on", 1), ("off", 0)))
def test_heartbeat_delivery_preserves_membership_board_history_and_direct_messages(
    notification_bus: Comms, delivery: str, publications: int,
) -> None:
    from agent_comms.cli_verbs.heartbeat_delivery import HeartbeatDeliveryCliCommand
    from agent_comms.registry.heartbeat_delivery import HeartbeatDelivery

    comms = notification_bus
    original = comms.registry.require("colleague")
    mode = FieldCodec.decode(type[HeartbeatDelivery], delivery)
    result = HeartbeatDeliveryCliCommand(name="colleague", delivery=mode).apply(comms)
    selected = comms.registry.require("colleague")
    assert result.delivery is selected.heartbeat_delivery is mode
    assert selected == replace(original, heartbeat_delivery=mode)
    assert FieldCodec.decode(Thread, FieldCodec.encode(selected)) == selected
    assert selected.for_registration(selected.name, selected).heartbeat_delivery is mode
    assert selected in comms.registry.snapshot().team_project.colleagues(comms.registry.snapshot())

    now = time.time()
    rule = ServicePassFailing(watch="delivery-setting", since=now, error="still visible", failed_at=now)
    hud = beat(comms, (rule,))
    messages = notifications(comms)
    assert len(messages) == publications
    for message in messages:
        assert FieldCodec.encode(message.task.handled) == FieldCodec.encode(hud.packets[0].handled)
        assert message.role.counts_as_conversation()
    assert TeamUpdate.latest(comms, selected.incarnation) == hud.packets
    again = beat(comms, (rule,))
    assert aged(again.packets[0].handled) == aged(hud.packets[0].handled)
    assert bool(notifications(comms)) is bool(publications)
    assert TeamFrame.current(comms, "colleague") == again
    direct = independent_instruction(comms, "ordinary direct request")
    assert direct.role.counts_as_conversation()
    assert direct.category is InboundCategory


def test_disabled_heartbeat_failure_retains_service_error_without_bus_publication(notification_bus):

    comms = notification_bus
    comms.threads.set_heartbeat_delivery("colleague", OffHeartbeatDelivery)
    failed_pass(comms, RuntimeError("muted service failure"))
    assert notifications(comms) == ()
    teammate = comms.registry.require("colleague").incarnation
    retained, = TeamUpdate.latest(comms, teammate)
    assert retained.handled == (RulePing.capture(retained_failure(comms)),)
    BrokenHeartbeat().failed(lambda: nullcontext(comms), RuntimeError("muted service failure"))
    assert TeamUpdate.latest(comms, teammate) == (retained,)
    assert TeamUpdate.published(comms, teammate) == ()
    assert notifications(comms) == ()
    comms.threads.set_heartbeat_delivery("colleague", OnHeartbeatDelivery)
    BrokenHeartbeat().failed(lambda: nullcontext(comms), RuntimeError("muted service failure"))
    resumed, = notifications(comms)
    assert resumed.role.counts_as_conversation()
    assert aged(resumed.task.handled) == aged(retained.handled)
    BrokenHeartbeat().failed(lambda: nullcontext(comms), RuntimeError("muted service failure"))
    assert len(notifications(comms)) == 1


def test_muted_pending_work_keeps_its_age_and_wakes_when_enabled(notification_bus):

    comms = notification_bus
    teammate = comms.registry.require("colleague")
    todo = comms.goals.todos.get("P1")
    rule = ReviewTimerUp(
        problem=todo.id, holder=comms.registry.require("worker").incarnation,
        order=todo.order.checked().id, since=RecordedEventTime(time.time() - 600),
    )
    comms.threads.set_heartbeat_delivery("colleague", OffHeartbeatDelivery)
    first = beat(comms, (rule,))
    again = beat(comms, (rule,))
    assert notifications(comms) == ()
    assert first.open_pings and again.open_pings
    assert aged(again.packets[0].handled) == aged(first.packets[0].handled)
    comms.threads.set_heartbeat_delivery("colleague", OnHeartbeatDelivery)
    enabled = beat(comms, (rule,))
    message, = notifications(comms)
    assert message.role.counts_as_conversation()
    assert aged(message.task.handled) == aged(first.packets[0].handled)
    assert enabled.teammate == teammate.incarnation
    assert TeamUpdate.latest(comms, teammate.incarnation) == enabled.packets


def test_muted_changed_quiet_display_publishes_once_when_enabled(notification_bus):

    comms = notification_bus
    first = beat(comms)
    original, = notifications(comms)
    comms.threads.set_heartbeat_delivery("colleague", OffHeartbeatDelivery)
    comms.registry.register_child("muted-fork", "colleague", ForkThread, comms.goals)
    muted = beat(comms)
    assert muted.display_facts != first.display_facts
    assert notifications(comms) == (original,)
    assert TeamFrame.current(comms, "colleague") == muted
    comms.threads.set_heartbeat_delivery("colleague", OnHeartbeatDelivery)
    enabled = beat(comms)
    assert enabled.display_facts == muted.display_facts
    _, resumed = notifications(comms)
    assert not resumed.role.counts_as_conversation()
    beat(comms)
    assert tuple(message.reference for message in notifications(comms)) == (
        original.reference, resumed.reference,
    )


def test_real_heartbeat_dispatches_watch_and_plan_roles_separately(notification_bus: Comms) -> None:
    heartbeat = TeamHeartbeat()
    prepared = heartbeat.prepare(notification_bus)
    inputs, = prepared
    item, = heartbeat.observe(notification_bus, prepared)
    assert item.frame.plan is inputs.plan
    assert item.frame.pulls is inputs.pulls
    assert (item.frame.live, item.frame.orphaned) == (inputs.live, inputs.orphaned)
    assert inputs.plan.reads
    assert any(plan.main == inputs.plan.main for plan in heartbeat.plans)


def owner(comms):
    """The owner's thread, which the heartbeat speaks under."""
    return comms.messaging.user_identity(Checkout(comms.root))


def notifications(comms, teammate="colleague"):
    page = MessagePageRequest(DMDisplayScope.capture(
        owner(comms).name, teammate, comms.registry.snapshot(),
    )).read_opened(comms.bus.bus)
    return tuple(message for message in page.messages if isinstance(message.task, TeamUpdate))


@contextmanager
def refused_publication(comms: Comms) -> Iterator[None]:
    """The real SQLite writer refuses inserts; the original transaction still rolls back."""
    bus = comms.bus.bus
    with bus.writing() as db:
        db.execute(
            f'CREATE TRIGGER refuse_publication BEFORE INSERT ON "{Message.table.declared_name}" '
            "BEGIN SELECT RAISE(ABORT, 'publication refused'); END"
        )
    try:
        yield
    finally:
        with bus.writing() as db:
            db.execute("DROP TRIGGER refuse_publication")


def lead_hud(comms, states=()):
    """The board the colleague reads, shown with the usage its last published board showed:
    tests compare its status fields with that publication, and the live account answers a
    second read with whatever its reset clock says by then."""
    return current_hud(comms, "colleague", capture_frame(comms, ProgressRead()), states,
                       TeamFrame.current(comms, "colleague").usage)


def assert_status_fields(message, hud):
    facts = hud.status_table(time.time()).facts
    assert all(f"{label}: {value}" in message.body
               for row in facts.rows for label, value in zip(facts.columns, row))


def beat(comms, rules=(), *, states=()):
    observed = TeamObservation(
        capture_frame(comms, ProgressRead()),
        rules, states)
    # Deliberately construct a fresh watcher on every pass, never reuse a cache.
    asyncio.run(ObservedHeartbeat(observed).run(lambda: nullcontext(comms)))
    return TeamFrame.current(comms, "colleague")


def test_expanded_unit_reads_original_orders_and_bounded_activity_then_refuses_stale_selection(
    notification_bus,
):
    comms = notification_bus
    recorded_turn(comms, (False,) * 10)
    original = (comms.root / 'state-source.jsonl').read_bytes()
    hud = beat(comms, states=state_cards(comms, TeammateState))
    ServicePassStore.at(comms.root).heartbeat_passed((hud.teammate,), (hud.teammate,))
    card = next(card for card in hud.units if card.unit.name == 'worker')
    operation = TeammateRead(frame_owner=hud.teammate, unit=card.unit,
                                  orders=tuple(dict.fromkeys(order.order for order in card.orders)))
    request = ReadOperation(str(comms.root), operation)
    direct = UiRead.serving(operation).read(comms)
    detail = pickle.loads(pickle.dumps(request.read(comms)))
    # The shared operation, the UI's root-bound envelope and the MCP boundary answer the same
    # selected card with the same bounded records.
    assert detail == direct
    assert invoke_tool(comms, TeammateRead.declared_name, FieldCodec.encode_fields(operation),
                       caller=ThreadCaller("colleague")) == FieldCodec.encode(direct)
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
        replace(request, operation=replace(operation, frame_owner=card.unit)),
    ):
        assert isinstance(stale.answer(comms), ReadRefused)
    checkout = comms.goals.todos.get('P1').order.checked().check.directory
    metadata = checkout / '.git'
    saved = checkout / 'saved-git'
    metadata.rename(saved)
    try:
        metadata.write_text('gitdir: ./missing-original-git\n')
        assert isinstance(request.answer(comms), ReadRefused)
        # MCP reaches the same nominal refusal, which the connector answers as a failed call.
        with pytest.raises(WorkBranchError):
            invoke_tool(comms, TeammateRead.declared_name, FieldCodec.encode_fields(operation),
                        caller=ThreadCaller("colleague"))
    finally:
        metadata.unlink()
        saved.rename(metadata)


class BrokenHeartbeat(TeamHeartbeat, declared_name=None):
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
    return ServicePassFailing(watch=BrokenHeartbeat.__name__, since=stored.since,
                              error=stored.error, failed_at=stored.failed_at)


def aged(pings):
    return tuple((ping, ping.since) for ping in pings)


def test_failed_heartbeat_pass_publishes_one_typed_packet_and_still_raises(notification_bus):
    comms = notification_bus
    raised = failed_pass(comms, RuntimeError("first wording"))
    assert str(raised) == "forced heartbeat pass failure"
    failure = retained_failure(comms)
    message, = notifications(comms)
    assert message.sender == owner(comms).name and message.target == "colleague"
    assert isinstance(message.task, TeamDecisions)
    assert message.task.decisions == message.task.handled == (RulePing.capture(failure),)
    assert failure.line() in message.body and "first wording" in message.body
    # The same occurrence again, from a fresh watcher with new wording, is not new.
    failed_pass(comms, ValueError("second wording"))
    assert retained_failure(comms).since == failure.since
    assert len(notifications(comms)) == 1


def test_failure_keeps_the_board_and_recovery_clears_only_the_failure(notification_bus):
    comms = notification_bus

    class RefusedRule(ServicePassFailing):
        def enforce(self, _comms):
            raise TeammateOccupied("another order already holds the selected teammate")

    recorded_turn(comms, (True, True))
    states = state_cards(comms, Spinning)
    rules = (RefusedRule(watch="refused", since=1.0, error="review pending", failed_at=1.0),
             ServicePassFailing(watch="independent", since=1.0, error="another source",
                                failed_at=1.0))
    beat(comms, rules, states=states)
    board, = notifications(comms)
    assert board.task.not_applied and len(board.task.handled) == 2

    failed_pass(comms, RuntimeError("source unreadable"))
    _, failing = notifications(comms)
    failure = RulePing.capture(retained_failure(comms))
    assert isinstance(failing.task, TeamDecisions)
    assert aged(failing.task.handled[:-1]) == aged(board.task.handled)
    assert failing.task.handled[-1] == failure and failing.task.decisions == (failure,)
    assert failing.task.waiting == ()
    assert failing.task.not_applied == board.task.not_applied

    ServicePassStore.at(comms.root).passed(BrokenHeartbeat.__name__)
    beat(comms, rules, states=state_cards(comms, Spinning))
    _, _, recovered = notifications(comms)
    assert isinstance(recovered.task, TeamDecisions)
    assert recovered.task.decisions == ()
    assert aged(recovered.task.handled) == aged(board.task.handled)

    # A new occurrence after the pass recovered is new, once.
    failed_pass(comms, RuntimeError("source unreadable again"))
    *_, again = notifications(comms)
    assert again.task.decisions == (RulePing.capture(retained_failure(comms)),)
    assert again.task.decisions != (failure,)
    failed_pass(comms, RuntimeError("source unreadable again"))
    assert len(notifications(comms)) == 4


def test_each_team_gets_its_own_failure_packet(notification_bus):
    comms = notification_bus
    ServicePassStore.at(comms.root).failed(BrokenHeartbeat.__name__, RuntimeError("down"))
    failure = retained_failure(comms)
    now = time.time()
    for name in ("colleague", "worker"):
        teammate = comms.registry.require(name).incarnation
        update, = TeamUpdate.report_failure(teammate, failure, (), published=(), now=now)
        TeamUpdateStore.record(comms.root, update)
        update.send(comms, ())
        latest, = TeamUpdate.latest(comms, teammate)
        assert latest.teammate == teammate
        assert latest.handled == (RulePing.capture(failure),)
        assert TeamUpdate.report_failure(
            teammate, failure, (latest,), published=TeamUpdate.published(comms, teammate),
            now=now + 1) == ()
    # The worker's later packet is not the colleague's board.
    colleague = comms.registry.require("colleague").incarnation
    lead_board, = TeamUpdate.latest(comms, colleague)
    assert lead_board.teammate == colleague


def independent_instruction(comms, body):
    """A taskless message the owner typed, under the same thread the heartbeat speaks in."""
    return comms.messaging.send_user_message(ThreadTarget("colleague"), body, worktree=Checkout(comms.root))


def everything_to_lead(comms):
    page = MessagePageRequest(DMDisplayScope.capture(
        owner(comms).name, "colleague", comms.registry.snapshot(),
    )).read_opened(comms.bus.bus)
    return {message.reference for message in page.messages}


def test_hidden_tool_time_keeps_publication_display_facts(notification_bus):
    comms = notification_bus
    recorded_turn(comms, (False,))
    hud = beat(comms, states=state_cards(comms, TeammateState))
    first, = notifications(comms)
    for _ in range(2):
        recorded_turn(comms, (False,))
        later = beat(comms, states=state_cards(comms, TeammateState))
        assert tuple(card.clock for card in hud.units) != tuple(card.clock for card in later.units)
        assert hud.display_facts == later.display_facts
        # A unit may still need a teammate action; a hidden clock is not a new display fact.
        assert first.task.publication == PublishedFacts.of(later.display_facts)


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
    assert isinstance(second.task, TeamDecisions)
    assert second.task.decisions == ()
    assert second.task.handled[0].since == first.task.handled[0].since
    hud = TeamFrame.current(comms, "colleague")
    assert hud.board == second.task.board(comms)
    assert len(hud.board) == 1

    publish(comms, "colleague", "worker", time.time())
    cleared = beat(comms, (rule,))
    assert not cleared.packets[0].role.counts_as_conversation()
    assert cleared.board == ()
    # ReviewTimerUp is already shown in the unit row, not as a separate alarm. Its
    # disposal advances the retained board without publishing an identical quiet display.
    assert cleared.display_facts == hud.display_facts
    assert len(notifications(comms)) == 2
    beat(comms, (rule,))
    assert len(notifications(comms)) == 2


def test_an_independent_heartbeat_message_never_replaces_the_board(notification_bus):
    comms = notification_bus
    failing = ServicePassFailing(watch="following-rule", since=time.time(), error="independent source",
                                 failed_at=time.time())
    beat(comms, (failing,))
    board, = notifications(comms)
    raw = independent_instruction(comms, "Heartbeat pass failed: an old raw alarm")
    colleague = comms.registry.require("colleague").incarnation
    assert TeamUpdate.latest(comms, colleague) == (board.task,)
    assert TeamUpdate.current_board(comms, comms.registry.require("colleague").incarnation) == board.task.board(comms)
    assert lead_hud(comms).board == board.task.board(comms)
    assert raw.reference in everything_to_lead(comms)
    # The next pass clears NEW without promoting a subjectless observation to STILL OPEN.
    beat(comms, (failing,))
    _, repeated = notifications(comms)
    assert isinstance(repeated.task, TeamUpdateNotice)
    assert repeated.task.handled == board.task.handled


def test_a_later_notice_is_the_baseline_and_keeps_the_lines_still_on_it(notification_bus):
    comms = notification_bus
    kept = ServicePassFailing(watch="kept-rule", since=time.time(), error="still failing",
                              failed_at=time.time())
    cleared = ServicePassFailing(watch="cleared-rule", since=time.time(), error="now fixed",
                                 failed_at=time.time())
    beat(comms, (kept, cleared))
    decisions, = notifications(comms)
    beat(comms, (kept,))
    _, notice = notifications(comms)
    assert isinstance(notice.task, TeamUpdateNotice)
    assert TeamUpdate.latest(comms, comms.registry.require("colleague").incarnation) == (notice.task,)
    assert RulePing.capture(kept) in notice.task.handled
    assert RulePing.capture(cleared) not in notice.task.handled
    assert decisions.reference in everything_to_lead(comms)


def test_a_raw_message_between_failures_does_not_restate_the_board(notification_bus):
    comms = notification_bus
    rules = (ServicePassFailing(watch="independent", since=1.0, error="another source",
                                failed_at=1.0),)
    beat(comms, rules)
    board, = notifications(comms)
    failed_pass(comms, RuntimeError("down"))
    independent_instruction(comms, "Heartbeat pass failed: down")
    failed_pass(comms, RuntimeError("still down"))
    assert len(notifications(comms)) == 2
    ServicePassStore.at(comms.root).passed(BrokenHeartbeat.__name__)
    beat(comms, rules)
    *_, recovered = notifications(comms)
    assert isinstance(recovered.task, TeamUpdateNotice)
    assert aged(recovered.task.handled) == aged(board.task.handled)
    assert recovered.task.not_applied == board.task.not_applied


def test_a_recreated_team_inherits_no_board(notification_bus):
    comms = notification_bus
    failing = ServicePassFailing(watch="following-rule", since=time.time(), error="independent source",
                                 failed_at=time.time())
    beat(comms, (failing,))
    board, = notifications(comms)
    worktree = comms.registry.require("colleague").worktree
    comms.registry.unregister("colleague")
    comms.registry.remove("colleague")
    comms.registry.declare(NativeThread("colleague", frozenset({"comms"}), worktree))
    reborn = comms.registry.require("colleague").incarnation
    assert reborn != board.task.teammate
    assert TeamUpdate.latest(comms, reborn) == ()
    assert TeamUpdate.latest(comms, board.task.teammate) == (board.task,)
    now = time.time()
    update, = TeamUpdate.report_failure(reborn, failing, (), published=(), now=now)
    TeamUpdateStore.record(comms.root, update)
    update.send(comms, ())
    latest, = TeamUpdate.latest(comms, reborn)
    assert latest.teammate == reborn and latest.handled == update.handled


def test_only_the_heartbeat_publishes_a_packet_for_its_exact_team(notification_bus):
    comms = notification_bus
    colleague = comms.registry.require("colleague").incarnation
    worker = comms.registry.require("worker").incarnation
    user = owner(comms)
    for sender, origin, teammate in (("worker", AgentOrigin(), colleague),
                                      (user.name, HumanOrigin.of(user), worker)):
        with pytest.raises(RelationViolationError):
            comms.bus.publisher.publish_ordinary(
                Message(sender=sender, target="colleague", body="forged board", type=InfoKind,
                        role=TeamUpdateNotice.role, task=TeamUpdateNotice(teammate, ())),
                origin=origin, destination=ThreadTarget("colleague"),
            )
    assert TeamUpdate.latest(comms, colleague) == ()
    assert TeamUpdate.latest(comms, worker) == ()


def test_failure_packet_follows_the_team_rename_alias(notification_bus):
    comms = notification_bus
    failed_pass(comms, RuntimeError("down"))
    first, = notifications(comms)
    comms.threads.rename_self("renamed-colleague", caller=ThreadCaller("colleague"))
    failed_pass(comms, RuntimeError("still down"))
    page = MessagePageRequest(DMDisplayScope.capture(
        owner(comms).name, "renamed-colleague", comms.registry.snapshot(),
    )).read_opened(comms.bus.bus)
    published = tuple(message for message in page.messages
                      if isinstance(message.task, TeamUpdate))
    assert tuple(message.reference for message in published) == (first.reference,)


def test_frame_puts_hands_first_and_folds_finished_phases_per_plan(notification_bus):
    """Ready and done-but-held phases colleague; finished phases are one count per plan."""
    from agent_comms.planning.plan_phases import (
        BusInterfacePhase,
        DesignReconciledPhase,
        OneOrderOwnerPhase,
        RecordPlanePhase,
        RetainedStorePhase,
        ReviewQueuePhase,
    )
    from agent_comms.team.team_hud_records import TeamPhase

    comms = notification_bus
    colleague = comms.registry.require("colleague").incarnation
    worker = comms.registry.require("worker").incarnation
    hud = TeamHud(teammate=colleague, units=(), phases=(
        TeamPhase(RetainedStorePhase, True, False, ()),
        TeamPhase(OneOrderOwnerPhase, True, False, ()),
        TeamPhase(DesignReconciledPhase, False, False, (worker,)),
        TeamPhase(RecordPlanePhase, False, True, ()),
        TeamPhase(ReviewQueuePhase, True, False, (worker,)),
        TeamPhase(BusInterfacePhase, True, False, ()),
    ), load=OverloadDegradation.from_orders((), 0, 0), forks=(worker,),
        usage=CodexUsage(UnreadableUsage(reason='not requested')), open_pings=())
    urgent = next(section for section in hud.groups if section.group is UrgentGroup)
    assert tuple(row.phase for row in urgent.records) == (RecordPlanePhase, ReviewQueuePhase)
    assert urgent.tables[0].record is type(hud.phases[0])
    done_by_plan = next(section for section in hud.groups
                        if section.group is PhasesDoneByPlanGroup)
    folded, = done_by_plan.records
    assert folded.phases == (RetainedStorePhase, OneOrderOwnerPhase)
    assert folded.done == 2
    # The plan still has urgent phases, so its heading must not call it finished.
    assert done_by_plan.heading() == "PHASES DONE BY PLAN · 1"
    # A plan with nothing left to do leaves the board, and the done counts fold to a heading.
    assert BusInterfacePhase.plan not in {record.plan for record in done_by_plan.records}
    assert BusInterfacePhase.declared_name not in "\n".join(hud.hud_text(time.time()))
    assert not PhasesDoneByPlanGroup.table_lines(table.folded_lines() for table in done_by_plan.tables)
    rendered = "\n".join(hud.hud_text(time.time()))
    assert all(phase.declared_name in rendered for phase in (RecordPlanePhase, ReviewQueuePhase))


def test_frame_keeps_original_ping_order_and_folded_idle_records(tmp_path):
    from agent_comms.nominal.field_codec import OrderId
    from agent_comms.registry.thread_identity import RecordedThreadIncarnation
    from agent_comms.ui_model.hud_groups import IdleGroup
    from agent_comms.ui_model.teammate_state import Idle, NoPendingClock, PendingClock, Stuck

    now = time.time()
    idle = Teammate(
        RecordedThreadIncarnation("idle-worker", 1.0), Idle, NoPendingClock(), (), 0, (), (),
    )
    stuck = replace(idle, unit=RecordedThreadIncarnation("stuck-worker", 2.0),
                    state=Stuck, clock=PendingClock(now - 3600, now - 3600))
    pings = tuple(replace(RulePing.capture(ReviewTimerUp(
        problem=problem, holder=unit.unit, order=OrderId(order),
        since=RecordedEventTime(now - age))), since=RecordedEventTime(now - age))
        for problem, unit, order, age in (
            ("P1", idle, "123456789abc", 120), ("P2", stuck, "abcdef123456", 240)))
    owner = RecordedThreadIncarnation("colleague", 3.0)
    packet = TeamDecisions(owner, pings, pings, ())
    hud = TeamHud(
        teammate=owner, units=(idle, stuck), phases=(),
        load=OverloadDegradation.from_orders((), 0, 0), forks=(),
        usage=CodexUsage(UnreadableUsage(reason="not requested")),
        packets=(packet,), open_pings=pings,
    )
    TeamFrame.replace_all(tmp_path, TeamPlan(hud.phases), (hud,))
    restored, = TeamFrame.read_all(tmp_path)
    assert restored == hud
    assert restored.open_pings == pings
    assert restored.board == tuple(ping.board_entry() for ping in pings)
    urgent, = (section for section in restored.groups if section.group is UrgentGroup)
    quiet, = (section for section in restored.groups if section.group is IdleGroup)
    assert urgent.records == (stuck,)
    assert quiet.records == (idle,)
    assert idle.unit.name in "\n".join(quiet.tables[0].lines())


def test_a_registered_fork_is_on_the_hud(notification_bus):
    comms = notification_bus
    fork = comms.registry.register_child("judge-fork", "colleague", ForkThread, comms.goals)
    hud = current_hud(comms, "colleague", capture_frame(comms, ProgressRead()), (), CodexConnection.usage())
    assert fork.incarnation in hud.forks


def test_quiet_completion_is_per_teammate_even_when_a_later_recipient_fails(
    notification_bus, monkeypatch,
):
    """One team board, delivered per teammate: a recipient failing after another was served
    leaves the served one's completion recorded."""
    comms = notification_bus
    colleague = comms.registry.require("colleague")
    missing = replace(comms.registry.require("worker"), name="unregistered-recipient")
    failing = ServicePassFailing(watch="following-rule", since=time.time(), error="independent source",
                                 failed_at=time.time())
    observed = TeamObservation(capture_frame(comms, ProgressRead()), (failing,), ())
    with monkeypatch.context() as patched:
        patched.setattr(TeamHeartbeat, "served", classmethod(lambda cls, registry, goals: (colleague, missing)))
        with pytest.raises(UnregisteredThreadError):
            asyncio.run(ObservedHeartbeat(observed).run(lambda: nullcontext(comms)))
    completed = ServicePassStore.at(comms.root).read().teammate_completed
    assert set(completed) == {colleague.incarnation}
    assert missing.incarnation not in completed
    # A quiet pass still completes and keeps the original alarm visibly open.
    beat(comms, (failing,))
    assert len(notifications(comms)) == 2
    # A pass with no board retires the frames and no teammate's completion.
    asyncio.run(ObservedHeartbeat().run(lambda: nullcontext(comms)))
    assert TeamFrame.read_all(comms.root) == ()
    assert set(ServicePassStore.at(comms.root).read().teammate_completed) == {colleague.incarnation}
    # Only the registry retiring the incarnation drops its completion.
    comms.registry.unregister("colleague")
    comms.registry.remove("colleague")
    asyncio.run(ObservedHeartbeat().run(lambda: nullcontext(comms)))
    assert not ServicePassStore.at(comms.root).read().teammate_completed


def test_recreated_watcher_retains_non_disposable_observation_without_nagging(notification_bus):
    """The original observation keeps its age, but is not an actionable open board line."""
    comms = notification_bus
    states = state_cards(comms, TeammateState)
    assert states and not any(state.attention() for state in states)
    failing = ServicePassFailing(watch="following-rule", since=time.time(), error="independent source",
                                 failed_at=time.time())
    beat(comms, (failing,), states=states)
    first, = notifications(comms)
    assert type(failing).declared_name in first.body
    assert not any(state.line() in first.body for state in states)
    hud = lead_hud(comms, states)
    assert_status_fields(first, hud)
    assert all(section.heading() in first.body for section in hud.groups)
    assert TeamUpdate.latest(comms, comms.registry.require("colleague").incarnation) == (first.task,)
    beat(comms, (failing,), states=state_cards(comms, TeammateState))
    _, unchanged = notifications(comms)
    assert unchanged.reference != first.reference
    assert isinstance(unchanged.task, TeamUpdateNotice)
    assert aged(unchanged.task.handled) == aged(first.task.handled)
    assert lead_hud(comms).board == ()
    from agent_comms.task_attachments.team_pings import WatchingAlarms

    section, = TeamFrame.current(comms, "colleague").alarms
    assert section.group is WatchingAlarms
    assert tuple(ping.text for ping in section.pings) == (failing.line(),)


def test_occupied_unit_refusal_is_published_and_later_rule_still_runs(notification_bus):
    comms = notification_bus
    enforced = []
    refusal = TeammateOccupied("another order already holds the selected teammate")

    class FollowingRule(ServicePassFailing):
        def enforce(self, _comms):
            enforced.append(self.watch)

    class OccupiedRule(FollowingRule):
        def enforce(self, _comms):
            super().enforce(_comms)
            raise refusal

    rules = (OccupiedRule(watch="occupied-unit", since=1.0, error="teammate already holds work",
                          failed_at=1.0),
             FollowingRule(watch="following-rule", since=1.0, error="another rule", failed_at=1.0))
    beat(comms, rules)

    assert enforced == ["occupied-unit", "following-rule"]
    message, = notifications(comms)
    assert len(message.task.not_applied) == 1
    assert refusal.explanation() in message.body
    assert rules[0].line() in message.body
    assert RulePing.capture(rules[0]) not in message.task.handled
    assert RulePing.capture(rules[1]) in message.task.handled
    assert comms.registry.require("colleague").incarnation in (
        ServicePassStore.at(comms.root).read().teammate_completed)


def test_rule_packets_compact_new_lines_keep_originals_then_repeat_without_a_new_wake(notification_bus):
    comms = notification_bus
    rules = tuple(ServicePassFailing(watch=f"watch-{index}", since=1.0, error="failed source read",
                                     failed_at=1.0)
                  for index in range(7))
    beat(comms, rules)
    first, = notifications(comms)
    assert isinstance(first.task, TeamDecisions)
    assert set(first.task.decisions) == set(first.task.handled) == {
        RulePing.capture(rule) for rule in rules}
    assert sum(len(section.pings) for section in lead_hud(comms).alarms) == len(rules)
    details = "\n".join(lead_hud(comms).alarm_detail(lead_hud(comms).alarms, time.time()))
    assert all(rule.line() in details for rule in rules)
    assert_status_fields(first, lead_hud(comms))
    beat(comms, rules)
    _, unchanged = notifications(comms)
    assert isinstance(unchanged.task, TeamUpdateNotice)
    assert TeamFrame.current(comms, "colleague").board == ()
    # Changed diagnostic wording is visible but not another failed occurrence.
    beat(comms, tuple(replace(rule, error="new diagnostic detail") for rule in rules))
    assert len(notifications(comms)) == 3
    changed = TeamFrame.current(comms, "colleague")
    assert aged(changed.packets[0].handled) == aged(first.task.handled)
    assert all("new diagnostic detail" in ping.text for ping in changed.packets[0].handled)


def test_resolution_is_passive_and_same_rule_can_recur(notification_bus):
    comms = notification_bus
    passes = ServicePassStore.at(comms.root)
    passes.failed("dependent-watch", RuntimeError("unreadable source"))
    rules = ServicePassFailing.violations(comms)
    beat(comms, rules)
    first, = notifications(comms)
    passes.passed("dependent-watch")
    beat(comms, ServicePassFailing.violations(comms))
    _, quiet = notifications(comms)
    assert isinstance(quiet.task, TeamUpdateNotice)
    assert_status_fields(quiet, lead_hud(comms))
    assert all(isinstance(decision.wake_mode, PassiveWake) for decision in quiet.decisions)
    # An identical quiet frame updates no bus history.
    beat(comms, ServicePassFailing.violations(comms))
    assert len(notifications(comms)) == 2
    # Use the same source occurrence to prove it was retired, not merely hidden.
    beat(comms, rules)
    _, _, repeated = notifications(comms)
    assert repeated.task.decisions == first.task.decisions


def test_latest_observation_follows_the_team_rename_alias(notification_bus):
    comms = notification_bus
    beat(comms, (ServicePassFailing(watch="watch", since=1.0, error="failed source read",
                                    failed_at=1.0),))
    first, = notifications(comms)
    comms.threads.rename_self("renamed-colleague", caller=ThreadCaller("colleague"))
    previous = TeamUpdate.latest(comms, comms.registry.require("renamed-colleague").incarnation)
    assert previous == (first.task,)


def test_new_local_commit_on_an_unpushed_branch_is_not_a_resolution(notification_bus, tmp_path):
    from agent_comms.team.publication_rules import UnpushedBranch

    comms = notification_bus
    holder = comms.registry.require("worker").incarnation
    first, moved = (UnpushedBranch(holder=holder, problem="P594", order="order",
                                   worktree=tmp_path, unpublished=UnpushedHead(head, commits, ""))
                    for head, commits in (("a" * 40, 54), ("b" * 40, 57)))
    assert RulePing.capture(first) == RulePing.capture(moved)
    beat(comms, (first,))
    beat(comms, (moved,))
    original, repeated = notifications(comms)
    assert isinstance(repeated.task, TeamDecisions)
    assert repeated.task.decisions == ()
    assert aged(repeated.task.handled) == aged(original.task.handled)


def test_failed_rule_scan_raises_to_the_heartbeat(notification_bus, request):
    comms = notification_bus
    frame = capture_frame(comms, ProgressRead())
    states = TeammateState.current(comms, frame)
    @dataclass(frozen=True)
    class FailedScan(UndatedStandingRule):
        @classmethod
        def scan_observed(cls, comms, observed, frame):
            raise IndexError("P729 isolated scan failure")

        def line(self):
            return "unreachable scan result"

    request.addfinalizer(FailedScan.withdraw)
    with pytest.raises(IndexError):
        StandingRule.broken(comms, observed=states, frame=frame)


async def test_team_page_reads_each_beats_frame_and_nothing_between(notification_bus):
    comms = notification_bus
    states = state_cards(comms, TeammateState)
    expected = await asyncio.to_thread(beat, comms, states=states)  # beat runs its own loop
    client, observer = Pipe()

    async def observed() -> tuple[TeamHudObserved, ...]:
        """One service pass, as run takes it: the service advances its own revision, and the
        board it prepares off the pass (in the task group only run enters) is awaited. The
        pass's other results, such as RevisionObserved, are not the team page's."""
        await service.observe(set())
        reading, = service.interest.observations.values()
        await asyncio.gather(*reading.preparation)
        sent = []
        while client.poll():
            sent.append(client.recv())
        return tuple(result for result in sent if isinstance(result, TeamHudObserved))

    with client, observer:
        service = ObservationService(comms, observer)
        try:
            async with service.tasks:
                service.interest.receive(ObserveTeamHud(), service)
                first, = await observed()
                assert first.huds == (expected,)
                assert len(await observed()) == 1  # the page's own preparation, once
                assert await observed() == ()
                await asyncio.to_thread(beat, comms, states=states)
                assert len(await observed()) == 1
        finally:
            service.retire()


def test_a_frame_this_reader_cannot_decode_reaches_the_page_not_the_observer(notification_bus):
    """An installed newer heartbeat writes a field an older Toad does not declare; the page
    shows the unreadable frame and the observer process, and Toad, keep running."""
    comms = notification_bus
    reading = ObserveTeamHud().observation()
    beat(comms, states=state_cards(comms, TeammateState))
    frame, = TeamFrame.matching_in(comms.root)
    written = json.loads(frame.read_text())
    frame.write_text(json.dumps({**written, "field_from_a_newer_build": []}))
    unreadable, = reading.read(comms, comms.views.revision())
    # The result crosses the observation process boundary by pickle.
    unreadable = pickle.loads(pickle.dumps(unreadable))
    assert isinstance(unreadable, observation.TeamHudUnreadable)
    assert "field_from_a_newer_build" in str(unreadable.error)
    beat(comms, states=state_cards(comms, TeammateState))
    recovered, = reading.read(comms, comms.views.revision())
    assert pickle.loads(pickle.dumps(recovered)).huds == TeamFrame.read_all(comms.root)


@dataclass
class PreparedInterest:
    readings: list = field(default_factory=list)

    def prepared(self, reading, service) -> None:
        self.readings.append(reading)


@dataclass
class PreparingService:
    """The parts of ObservationService a board preparation touches, recording what it sends."""

    comms: object
    revision: object
    sent: list = field(default_factory=list)
    interest: PreparedInterest = field(default_factory=PreparedInterest)

    def send(self, result) -> None:
        self.sent.append(result)


@pytest.mark.parametrize("failure, outcome", [
    (DeferredRead("the board's stores are mid-write"), "again"),
    (ValueError("a heartbeat frame this build cannot decode"), "unreadable"),
    (RuntimeError("a defect in the board read"), "raised"),
])
def test_preparing_the_board_answers_each_read_outcome_through_its_owner(
        notification_bus, failure, outcome):
    """prepare() caught every Exception and called it unreadable history: a deferred read was
    shown as broken instead of retried, and a real defect was rendered as a saved-data error.
    It now reads through UnreadableHistory.outcome, the owner read() already uses."""
    comms = notification_bus
    reading = ObserveTeamHud().observation()
    service = PreparingService(comms, comms.views.revision())
    reading.prepared_revision = service.revision

    def failing_board(_comms):
        raise failure

    reading.read_board = failing_board
    if outcome == "raised":
        with pytest.raises(RuntimeError, match="a defect in the board read"):
            asyncio.run(reading.prepare(service))
        return
    asyncio.run(reading.prepare(service))
    assert service.interest.readings == []
    if outcome == "again":
        assert service.sent == []
        assert isinstance(reading.prepared_revision, NoRevision)
    else:
        unreadable, = service.sent
        assert isinstance(unreadable, observation.TeamHudUnreadable)
        assert "cannot decode" in str(unreadable.error)


def test_codex_usage_decodes_window_and_keeps_refusal_visible():
    import json
    from datetime import datetime

    from agent_comms.cli_bridges.codex_protocol import RateLimitsResponse
    from agent_comms.nominal.field_codec import FieldCodec

    reply = FieldCodec.decode(RateLimitsResponse, json.loads('''{
        "rateLimits": {"primary": {
            "usedPercent": 37, "windowDurationMins": 10080, "resetsAt": 1793513456
        }, "planType": "pro"}
    }'''))
    usage = CodexUsage(reply.rate_limits)
    reset = datetime.fromtimestamp(1793513456).strftime('%a %b %d %H:%M')
    assert usage.line() == f'Codex (pro) usage 37% weekly, resets {reset}'
    assert not usage.needs_hand()
    assert FieldCodec.decode(CodexUsage, FieldCodec.encode(usage)) == usage
    refused = CodexUsage(UnreadableUsage(reason='Codex: account unavailable'))
    assert refused.line() == 'Codex usage unreadable: Codex: account unavailable'
    assert refused.needs_hand()
    assert FieldCodec.decode(CodexUsage, FieldCodec.encode(refused)) == refused


def test_waiting_notification_keeps_actionable_subjects_overflow_and_original_evidence(notification_bus):
    comms = notification_bus
    from agent_comms.projects.head_review import PullRequestHead
    from agent_comms.projects.repository_name import RepositoryName
    from agent_comms.team.standing_rules import OrderlessUnreviewedHead

    rules = tuple(OrderlessUnreviewedHead(
        request=PullRequestHead(RepositoryName.from_wire("OpenHCSDev/toad"), 100 + index, "a" * 40),
        due_at=1.0,
    ) for index in range(12))
    beat(comms, rules)
    original, = notifications(comms)
    packet = TeamDecisions(
        original.task.teammate, original.task.handled, (), original.task.handled,
    )
    hud = replace(lead_hud(comms), packets=(packet,), open_pings=packet.handled)
    message, = packet.send(comms, hud.hud_text(time.time()))
    for frame in (lead_hud(comms), hud):
        now = time.time()
        original_lines = frame.alarm_detail(frame.alarms, now)
        assert all(section.heading() not in original_lines for section in frame.alarms)
        assert all(rule.line() in "\n".join(original_lines) for rule in rules)
    assert len(message.task.handled) == 12
    assert {entry.text for entry in message.task.board(comms)} == {rule.line() for rule in rules}
    assert len(pickle.loads(pickle.dumps(message.task)).handled) == 12
    from agent_comms.nominal.text_digest import TextDigest

    swapped = (*packet.handled[:-1], replace(packet.handled[-1], observation=TextDigest.of("other observation")))
    replacement = replace(packet, handled=swapped, waiting=swapped)
    changed = replace(hud, packets=(replacement,), open_pings=swapped)
    assert hud.alarm_text(hud.alarms, 1000) == changed.alarm_text(changed.alarms, 1000)
    assert hud.display_facts != changed.display_facts


def test_failed_quiet_publication_retries_identical_display_until_committed(notification_bus):
    """A real SQLite refusal rolls back publication, not the disposable frame write."""
    from agent_comms.task_attachments.team_pings import PublishedFacts

    comms = notification_bus
    first_hud = beat(comms)
    first, = notifications(comms)
    assert isinstance(first.task, TeamUpdateNotice)
    assert first.task.publication == PublishedFacts.of(first_hud.display_facts)
    comms.registry.register_child("quiet-fork", "colleague", ForkThread, comms.goals)
    with refused_publication(comms), pytest.raises(HumanSendRefusedError):
        beat(comms)
    owner = comms.registry.require("colleague").incarnation
    unsent = TeamFrame.current(comms, "colleague")
    assert first_hud.display_facts != unsent.display_facts
    assert TeamUpdate.latest(comms, owner) == unsent.packets
    assert TeamUpdate.published(comms, owner) == (first.task,)
    assert TeamUpdate.published(comms, owner)[0].publication == PublishedFacts.of(first_hud.display_facts)
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
    ServicePassStore.at(comms.root).failed("independent-watch", OSError("new recorded failure"))
    failed = beat(comms)
    assert failed.display_facts != retried.display_facts
    assert "new recorded failure" in failed.status_strip(time.time())
    assert len(notifications(comms)) == 3


def test_unsent_new_alert_remains_new_on_retry_without_resetting_its_age(notification_bus):
    comms = notification_bus
    beat(comms)
    first, = notifications(comms)
    rule = ServicePassFailing(
        watch="unsent-new-alert", since=time.time(), error="a source failed", failed_at=time.time(),
    )
    with refused_publication(comms), pytest.raises(HumanSendRefusedError):
        beat(comms, (rule,))
    owner = comms.registry.require("colleague").incarnation
    observed, = TeamUpdate.latest(comms, owner)
    assert observed.handled == (RulePing.capture(rule),)
    assert TeamUpdate.published(comms, owner) == (first.task,)
    beat(comms, (rule,))
    _, announced = notifications(comms)
    assert announced.role.counts_as_conversation()
    assert announced.task.new_pings() == observed.handled
    assert aged(announced.task.handled) == aged(observed.handled)


def test_subject_evidence_is_shared_without_losing_each_pings_start(notification_bus):
    from agent_comms.heartbeat_subjects import TeamHistory, TeammateSubject
    from agent_comms.task_attachments.team_pings import TeamDelivery
    from test_holder_standing_rules import publish

    comms = notification_bus
    owner = comms.registry.require("colleague").incarnation
    worker = comms.registry.require("worker").incarnation
    now = time.time()
    action = publish(comms, "colleague", "worker", now - 300)
    subject = TeammateSubject(worker)
    todo = comms.goals.todos.get("P1")
    rule = ReviewTimerUp(problem=todo.id, holder=worker, order=todo.order.checked().id,
                         since=RecordedEventTime(now - 600))
    old = replace(RulePing.capture(rule), subjects=(subject,), since=RecordedEventTime(now - 600))
    new = replace(old, since=RecordedEventTime(now - 200))
    packet = TeamDecisions(owner, (old, new), (), ())
    history = TeamHistory.read(comms, frozenset({"colleague"}))
    assert history.action_at(subject) == RecordedEventTime(action.timestamp)
    assert old.acted(history) and not new.acted(history)
    opened = packet.open_pings(history)
    assert opened == (new,) and opened[0].since == new.since
    hud = TeamHud.of(comms, capture_frame(comms, ProgressRead()), (),
                          delivery=TeamDelivery(packet, opened), usage=CodexConnection.usage())
    assert hud.open_pings is opened
    assert hud.board == (new.board_entry(),)
    # A later original action changes a fresh beat, never the already captured evidence.
    publish(comms, "colleague", "worker", now)
    assert not new.acted(history)
    assert new.acted(TeamHistory.read(comms, frozenset({"colleague"})))


def test_retained_packet_without_display_witness_keeps_original_message_identity(notification_bus):
    from agent_comms.task_attachments.team_pings import PublishedFacts, UnpublishedFacts

    comms = notification_bus
    hud = beat(comms)
    message, = notifications(comms)
    encoded = message.to_wire()
    # The original retained packet has no evidence for display facts; the default says so.
    del encoded["task"]["publication"]
    retained = Message.from_wire(encoded)
    assert retained.message_id == message.message_id
    assert retained.reference == message.reference
    assert retained.body == message.body
    assert retained.task.publication == UnpublishedFacts()
    facts = PublishedFacts.of(hud.display_facts)
    assert retained.task.should_publish(facts, (retained.task,))
    assert not message.task.should_publish(facts, (message.task,))
    assert Message.from_wire(retained.to_wire()).message_id == message.message_id


def test_a_stopped_session_is_on_the_team_only_while_an_open_request_waits_on_it(
    notification_bus,
):
    """No colleague: the team is every running colleague and every colleague an open request waits
    on, so the board never grows with every session the registry has held. A stopped
    colleague with nothing open is off the board; one of Tristan's asks puts it back on."""
    comms = notification_bus
    worker = comms.registry.require("worker")
    assert not worker.process_alive
    assert worker.incarnation not in comms.goals.todos.requesters()
    served = {thread.name for thread in TeamHeartbeat.served(comms.registry, comms.goals)}
    assert "colleague" in served and "worker" not in served
    comms.goals.todos.add_request(worker.incarnation, "The stopped worker's own ask.")
    assert "worker" in {thread.name
                        for thread in TeamHeartbeat.served(comms.registry, comms.goals)}


def test_a_closed_session_leaves_the_board_for_the_live_teammates(notification_bus):
    """One teammate's process going away must not empty the heartbeat for the others."""
    comms = notification_bus
    comms.registry.register(replace(
        comms.registry.require("worker"), tags=frozenset({"comms", TeammatesTag.declared_name}),
        process_identity=ProcessIdentity.capture(os.getpid())))
    comms.registry.register(replace(comms.registry.require("colleague"),
                                    process_identity=NoProcess()))
    colleague = comms.registry.require("colleague")
    assert not colleague.process_alive
    assert {thread.name for thread in TeamHeartbeat.served(comms.registry, comms.goals)} >= {"colleague", "worker"}
    beat(comms, (ServicePassFailing(watch="following-rule", since=time.time(),
                                    error="independent source", failed_at=time.time()),))
    worker = comms.registry.require("worker").incarnation
    assert TeamUpdate.latest(comms, worker)
    assert TeamFrame.current(comms, "worker").teammate == worker


def test_one_pass_gives_every_team_its_own_packet(notification_bus):
    """One board read, one packet per live independent colleague under its own incarnation.
    Execution owns independence (#3824): a thread without the teammates tag still gets one, and
    a fork of a colleague (carrying its tags, acting for it) gets none."""
    comms = notification_bus
    live = ProcessIdentity.capture(os.getpid())
    for name, tags in (("worker", {"comms", TeammatesTag.declared_name}), ("outsider", {"comms"})):
        comms.registry.register(replace(comms.registry.require(name), tags=frozenset(tags),
                                        process_identity=live))
    fork = comms.registry.register_child("worker-fork", "worker", ForkThread, comms.goals)
    comms.registry.register(replace(fork, process_identity=live))
    assert TeammatesTag.declared_name in comms.registry.require("worker-fork").tags
    failing = ServicePassFailing(watch="following-rule", since=time.time(), error="independent source",
                                 failed_at=time.time())
    beat(comms, (failing,))
    lead_packet, = TeamUpdate.latest(comms, comms.registry.require("colleague").incarnation)
    worker = comms.registry.require("worker").incarnation
    outsider = comms.registry.require("outsider").incarnation
    for colleague in (worker, outsider):
        packet, = TeamUpdate.latest(comms, colleague)
        assert packet.teammate == colleague
        assert packet.handled == lead_packet.handled == (RulePing.capture(failing),)
    assert TeamUpdate.latest(comms, comms.registry.require("worker-fork").incarnation) == ()
    served = (lead_packet.teammate, worker, outsider)
    assert set(ServicePassStore.at(comms.root).read().teammate_completed) == set(served)
    for recipient in served:
        hud = TeamFrame.current(comms, recipient.name)
        assert hud.teammate == recipient
        assert hud.packets == TeamUpdate.latest(comms, recipient)
        assert len(notifications(comms, recipient.name)) == 1
    # Every recipient owns its quiet-publication receipt; the prepared board is shared.
    beat(comms, (failing,))
    beat(comms, (failing,))
    for recipient in served:
        first, quiet = notifications(comms, recipient.name)
        assert isinstance(quiet.task, TeamUpdateNotice)
        assert quiet.task.teammate == recipient
        assert aged(quiet.task.handled) == aged(first.task.handled)
        assert TeamFrame.current(comms, recipient.name).packets == (quiet.task,)


def test_fork_cli_inherits_channels_without_a_rank_exception(notification_bus):
    comms = notification_bus
    parent = comms.registry.require('worker')
    tag = TeammatesTag.declared_name
    comms.registry.register(replace(parent, tags=parent.tags | {'comms', tag}))
    parent = comms.registry.require(parent.name)
    command = ForkCliCommand.thread_binding(parent)
    assert command.tags.for_new() == parent.tags
    explicit = replace(command, tags=GivenTags(parent.tags))
    assert tag in explicit.tags.for_new()


def test_enrollment_and_independent_execution_not_tags_determine_shared_participation(notification_bus, tmp_path):
    """Every registered binding participates; only real in-turn executions are excluded
    from the independent collaborator roster. Copying channels creates no parent authority."""
    comms = notification_bus
    tag = TeammatesTag.declared_name
    for name in ("colleague", "worker"):
        thread = comms.registry.require(name)
        comms.registry.register(replace(thread, tags=thread.tags | {tag}))
    declared = comms.registry.register_child("declared-fork", "worker", ForkThread, comms.goals)
    comms.registry.register(replace(declared, tags=declared.tags | {tag}))
    comms.registry.register_child("copied-fork", "worker", ForkThread, comms.goals)
    worker = comms.registry.require("worker")
    bound = comms.registry.register_bound_thread(
        "bound-unit", "worker", worker.worktree, ProcessIdentity.capture(os.getpid()))
    assert tuple(bound.parent.names()) == ()
    assert bound.acting_identity == bound.incarnation

    def commands(name):
        claim = WatchClaim(name, home=tmp_path)
        claim.enrol(WatchRecord(name, str(comms.root), (), tmp_path))
        return claim.state().commands()

    assert all(commands(name) for name in (
        "colleague", "worker", "outsider", "declared-fork", "copied-fork", "bound-unit"))
    assert not commands("never-registered")
    snapshot = comms.registry.snapshot()
    independent = {peer.name for peer in snapshot.team_project.colleagues(snapshot)}
    assert {"colleague", "worker", "outsider", "bound-unit"} <= independent
    assert {"declared-fork", "copied-fork"}.isdisjoint(independent)
    assert {"comms", tag} <= snapshot.require("copied-fork").tags


def test_a_board_packet_files_under_heartbeats_while_the_owners_own_words_stay_inbound(notification_bus):
    comms = notification_bus
    colleague = comms.registry.require("colleague").incarnation
    user = owner(comms)
    board = Message(sender=user.name, target="colleague", body="board", type=InfoKind,
                    role=TeamDecisions.role, task=TeamDecisions(colleague, (), (), ()))
    spoken = Message(sender=user.name, target="colleague", body="his words", type=InfoKind,
                     role=ConversationRole())
    assert board.category is HeartbeatCategory
    assert spoken.category is InboundCategory
    assert IncomingTranscript.of(board, reader="colleague").category is HeartbeatCategory
    assert HeartbeatCategory in all_categories()


def test_an_archived_lead_keeps_its_board_and_the_pass_completes(notification_bus):
    """2026-11-02: from 14:49 every heartbeat pass died on 'Initial direct target must be visible'
    sending the archived colleague its packet, so no teammate had a board."""
    from agent_comms.registry.thread_status import ArchivedThreadStatus

    comms = notification_bus
    comms.registry.register(comms.registry.require("colleague"), ArchivedThreadStatus())
    assert beat(comms) is not None
    assert comms.registry.status("colleague") == ArchivedThreadStatus()
