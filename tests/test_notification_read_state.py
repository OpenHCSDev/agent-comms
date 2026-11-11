import pickle
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.acp_extension import GoalSnapshotReply, TranscriptSnapshotUpdate
from agent_comms.comms import Comms
from agent_comms.coordination.errors import ReloadRequired
from agent_comms.execution.transcript_sources import (
    EarlierThan,
    NativeTranscriptCursor,
)
from agent_comms.history_views import HistoryViews
from agent_comms.messaging.bus_read import (
    BusRead,
    OwnerScopedRead,
    UiRead,
)
from agent_comms.messaging.bus_read import BusRead, OwnerScopedRead, UiRead
from agent_comms.messaging.message_notification import NotificationPass
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.notification_reads import (
    LiveNotificationRead,
    NotificationRead,
    RecordedNotificationRead,
)
from agent_comms.messaging.page_traversal import HistorySourceKey
from agent_comms.messaging.revision_views import (
    ActivityView,
    NotificationEvidenceView,
    NotificationsView,
    RegistrationsView,
    SidebarView,
    StatusView,
    StoresView,
)
from agent_comms.nominal.field_codec import FieldCodec, MessageId, OrderId
from agent_comms.notification_read import NotificationsRead, UnreadableNotificationsRead
from agent_comms.presentation import NoRevision, WireRevision
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_names import DeletedThread
from agent_comms.registry.thread_identity import (
    AgentThreadRole,
    RecordedThreadIncarnation,
    UserThreadRole,
)
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.private_path import FileRevision, MissingFile
from agent_comms.team.team_hud_records import TeammateDetail
from agent_comms.transcript.transcripts import RecordedPageSource
from agent_comms.transport.request import ToolRequest
from agent_comms.transport.tool_declaration import NativeRequestTools
from agent_comms.transport.tools.reads import (
    InputDeliveryRead,
    OwnerGoalRead,
    TeammateRead,
    TranscriptPageRead,
)
from agent_comms.transport.tools.served import CommsMessageOutcomesTool
from agent_comms.ui_model.observation import (
    NotificationBatch,
    NotificationsObserved,
    ObserveNotifications,
    ReadNotifications,
    ReadOperation,
    ReadOwnerOperation,
    ReadTranscript,
    ReadTranscriptUpdate,
    RefreshTranscript,
    SubscribeTranscript,
)


def test_notification_read_states_use_the_request_revision_contract(comms):
    original = WireRevision.empty()
    later = replace(original, expiry_tick=original.expiry_tick + 60)
    live = LiveNotificationRead(())
    recorded = RecordedNotificationRead((), HistorySourceKey("archive"))
    settled = NotificationsRead(original, {})
    assert not live.changed_since(comms, settled, NotificationPass(comms, original))
    assert not live.changed_since(comms, settled, NotificationPass(comms, later))
    assert not recorded.changed_since(comms, settled, NotificationPass(comms, later))
    assert live.changed_since(comms, UnreadableNotificationsRead(original), NotificationPass(comms, later))
    assert not live.changed_since(comms, UnreadableNotificationsRead(original), NotificationPass(comms, original))


TEAM = (("sender", AgentThreadRole), ("worker", AgentThreadRole), ("human", UserThreadRole),
        ("archived", AgentThreadRole), ("gone", AgentThreadRole))


def declare_team(comms: Comms, tmp_path: Path) -> MessageReference:
    """One real #team message from an agent sender, seen by a human and by three native
    agent recipients; one of those is archived or deleted afterwards. The window names only
    native recipients, so the sender and the human are never watched."""
    comms.channels.create_tag("team")
    for name, role in TEAM:
        comms.registry.declare(NativeThread(name, frozenset({"team"}), Checkout(tmp_path), role=role))
    return comms.messaging.send("sender", "#team", "Who is still here?").message.reference


def settled_read(comms: Comms, reference: MessageReference) -> tuple[WireRevision, NotificationsRead]:
    revision = comms.views.revision()
    window = LiveNotificationRead((reference,)).read(comms)
    return revision, NotificationsRead.of(revision, comms, {}, window)


def test_the_window_names_native_recipients_and_activity_holds_only_projected_ones(comms, tmp_path):
    reference = declare_team(comms, tmp_path)
    comms.registry.archive("archived")
    comms.registry.remove("gone")
    revision, settled = settled_read(comms, reference)

    names = settled.notification_window.activity_names()
    assert names == {"worker", "archived", "gone"}
    assert not names & {"sender", "human"}
    assert settled.activity.keys() == names - {"archived"}
    assert settled.activity["gone"] == DeletedThread("gone")
    assert settled.activity["worker"] == comms.views.observed_activity(revision, ("worker",))["worker"]
    assert not settled.live_changed(comms, NotificationPass(comms, revision))


def test_an_archived_participant_becoming_visible_changes_the_read(comms, tmp_path):
    reference = declare_team(comms, tmp_path)
    comms.registry.archive("archived")
    revision, settled = settled_read(comms, reference)
    assert "archived" not in settled.activity

    comms.registry.declare(NativeThread("archived", frozenset({"team"}), Checkout(tmp_path)))
    later = comms.views.revision()
    assert later.changed_since(ActivityView(), revision)
    assert settled.live_changed(comms, NotificationPass(comms, later))


def test_a_participant_deleted_after_the_read_changes_it(comms, tmp_path):
    reference = declare_team(comms, tmp_path)
    revision, settled = settled_read(comms, reference)
    assert "gone" in settled.activity and settled.activity["gone"] != DeletedThread("gone")

    comms.registry.remove("gone")
    assert settled.live_changed(comms, NotificationPass(comms, comms.views.revision()))


def test_notification_read_only_acquires_activity_when_its_source_changed(comms, tmp_path):
    reference = declare_team(comms, tmp_path)
    comms.registry.archive("archived")
    original, settled = settled_read(comms, reference)
    assert settled.notification_window.activity_names() - settled.activity.keys() == {"archived"}
    marker = tmp_path / 'changed-source'
    marker.write_text('changed')
    source = FileRevision.capture(marker).revisions()
    assert replace(original, history=source).changed_since(NotificationsView(), original)
    calls = 0

    def count_reads(frame, event, argument):
        nonlocal calls
        if event == 'call' and frame.f_code is HistoryViews.observed_activity.__code__:
            calls += 1

    previous = sys.getprofile()
    sys.setprofile(count_reads)
    try:
        assert not settled.live_changed(comms, NotificationPass(comms, replace(original, reads=source)))
        assert calls == 0
        assert not settled.live_changed(comms, NotificationPass(comms, replace(original, activity=source)))
        assert calls == 1
    finally:
        sys.setprofile(previous)


def test_unrelated_registry_change_keeps_the_notification_window(comms: Comms, tmp_path: Path):
    marker = tmp_path / 'registry-revision'
    marker.write_text('unrelated registration change')
    original = WireRevision.empty()
    later = replace(original, registry=FileRevision.capture(marker).revisions())
    settled = NotificationsRead(original)

    assert later.changed_since(ActivityView(), original)
    assert not settled.live_changed(comms, NotificationPass(comms, later))


def batch_of(request: ReadNotifications) -> NotificationBatch:
    state = NotificationsRead(WireRevision.empty())
    return NotificationBatch(request, state, NotificationsObserved(WireRevision.empty(), request, {}))


def test_changed_notification_interest_keeps_only_batches_a_request_still_shows():
    shown, scrolled_away, revealed = (MessageReference(seq, MessageId.mint(f"m{seq}")) for seq in (1, 2, 3))
    view = ReadNotifications("root", LiveNotificationRead((shown, scrolled_away)))
    kept, dropped = batch_of(view.showing((shown,))), batch_of(view.showing((scrolled_away,)))
    previous = ObserveNotifications(frozenset((view,))).observation()
    previous.batches = (kept, dropped)
    requested = ObserveNotifications(frozenset((view.showing((shown, revealed)),))).observation()

    assert previous.for_interest(requested) is requested
    assert requested.batches == (kept,)


def test_a_scrolled_view_reads_only_the_messages_no_batch_covers(comms, tmp_path):
    """An outcome belongs to its message: scrolling one row reads that row, not the page."""
    first = declare_team(comms, tmp_path)
    second, third = (comms.messaging.send("sender", "#team", text).message.reference
                     for text in ("Second?", "Third?"))
    view = ReadNotifications(str(comms.root), LiveNotificationRead((first, second)))
    reading = ObserveNotifications(frozenset((view,))).observation()
    answers = list(reading.read(comms, comms.views.revision()))
    assert [answer.request for answer in answers] == [view]
    assert answers[0].outcomes.keys() == {(first.seq, first.message_id), (second.seq, second.message_id)}

    scrolled = view.showing((second, third))
    moved = ObserveNotifications(frozenset((scrolled,))).observation()
    assert reading.for_interest(moved) is moved
    answers = list(moved.read(comms, comms.views.revision()))

    assert [batch.request.operation.references for batch in moved.batches] == [(first, second), (third,)]
    assert [answer.request for answer in answers] == [scrolled]
    assert answers[0].outcomes.keys() == {(second.seq, second.message_id), (third.seq, third.message_id)}
    assert list(moved.read(comms, comms.views.revision())) == []


REFERENCES = (MessageReference(1, MessageId.mint("original")),)


@pytest.mark.parametrize("operation", [
    LiveNotificationRead(REFERENCES), RecordedNotificationRead(REFERENCES, HistorySourceKey("archive")),
], ids=["live", "recorded"])
def test_notification_operations_are_root_free_family_records(operation: NotificationRead) -> None:
    assert operation.references == REFERENCES
    encoded = FieldCodec.encode(operation)
    assert "root" not in encoded and "request_id" not in encoded
    decoded = FieldCodec.decode(NotificationRead, encoded)
    assert decoded == operation and decoded.references == REFERENCES
    assert FieldCodec.decode(UiRead, encoded) == operation
    request = ReadNotifications("root", operation, request_id=4)
    transported = pickle.loads(pickle.dumps(request))
    assert transported == request and hash(transported) == hash(request)
    assert {transported: "held"}[request] == "held" and len({request, transported}) == 1


def test_root_request_references_and_archive_each_distinguish_a_notification_request():
    other_reference = (MessageReference(2, MessageId.mint("other")),)
    request = ReadNotifications("root", RecordedNotificationRead(REFERENCES, HistorySourceKey("archive")), request_id=4)
    distinct = (
        replace(request, root="other-root"),
        replace(request, request_id=5),
        replace(request, operation=RecordedNotificationRead(other_reference, HistorySourceKey("archive"))),
        replace(request, operation=RecordedNotificationRead(REFERENCES, HistorySourceKey("other-archive"))),
        replace(request, operation=LiveNotificationRead(REFERENCES)),
    )
    assert len({request, *distinct}) == 1 + len(distinct)


def test_equal_requests_keep_their_batches_and_a_new_archive_drops_them():
    kept = ReadNotifications("root", RecordedNotificationRead(REFERENCES, HistorySourceKey("archive")))
    replaced = ReadNotifications("root", RecordedNotificationRead(REFERENCES, HistorySourceKey("detached")))
    reattached = ReadNotifications("root", RecordedNotificationRead(REFERENCES, HistorySourceKey("attached")))
    previous = ObserveNotifications(frozenset((kept, replaced))).observation()
    batch = batch_of(kept)
    previous.batches = (batch, batch_of(replaced))
    requested = ObserveNotifications(frozenset((
        ReadNotifications("root", RecordedNotificationRead(REFERENCES, HistorySourceKey("archive"))), reattached,
    ))).observation()

    assert previous.for_interest(requested) is requested
    assert requested.batches == (batch,)
    assert requested.batches[0] is batch


def test_one_operation_in_two_root_envelopes_keeps_root_validation(comms):
    operation = LiveNotificationRead(REFERENCES)
    ours = ReadNotifications(str(comms.root), operation)
    foreign = ReadNotifications(str(comms.root.parent / "another-wire"), operation)
    assert ours.operation is foreign.operation and ours != foreign
    ours.require_root(comms)
    with pytest.raises(ReloadRequired):
        foreign.require_root(comms)


def test_notification_operations_are_ui_reads_and_not_tools():
    assert BusRead.members_with(NotificationRead) == (LiveNotificationRead, RecordedNotificationRead)
    assert set(BusRead.members_with(NotificationRead)) <= set(BusRead.members_with(UiRead))
    tools = (*ToolRequest.members_with(ToolRequest), *NativeRequestTools.members_with(NativeRequestTools))
    assert not any(issubclass(tool, NotificationRead) for tool in tools)
    assert CommsMessageOutcomesTool in ToolRequest.members_with(ToolRequest)
    assert CommsMessageOutcomesTool in NativeRequestTools.members_with(NativeRequestTools)


def test_the_notification_decoder_refuses_an_operation_outside_its_family():
    with pytest.raises(ValueError):
        FieldCodec.decode(NotificationRead, FieldCodec.encode(InputDeliveryRead()))


def test_activity_views_receive_expiry_without_expiring_store_or_evidence_views():
    original = WireRevision.empty()
    later = replace(original, expiry_tick=60)
    for view in (ActivityView(), SidebarView(), StatusView(), NotificationsView()):
        assert later.changed_since(view, original)
    assert not later.changed_since(StoresView(), original)
    assert not later.changed_since(NotificationEvidenceView(), original)


def test_first_revision_requires_reads_even_for_unchanged_empty_stores():
    unseen = NoRevision()
    known = WireRevision.empty()
    assert unseen.changed_since(StoresView(), known)
    assert unseen.changed_since(RegistrationsView(), known)
    assert known.changed_since(RegistrationsView(), unseen)
    assert unseen.changed_since(RegistrationsView(), unseen)
    assert unseen.changed_since(SidebarView(), known)
    assert not known.changed_since(StoresView(), known)
    assert not known.changed_since(RegistrationsView(), known)
    assert not known.changed_since(SidebarView(), known)


def test_notification_rank_overrides_follow_the_mro_without_changing_parent_ranks():
    from agent_comms.messaging.notification_progress import (
        ActiveDelivery,
        DeliveryRank,
        SelectedConsumedProgress,
        SendingProgress,
        SentProgress,
        SettledDelivery,
        ShownToModelProgress,
        WaitingDelivery,
    )

    assert SentProgress(100).rank() == DeliveryRank.position_of(WaitingDelivery)
    assert ShownToModelProgress(100).rank() == DeliveryRank.position_of(SettledDelivery)
    assert SendingProgress(100).rank() == DeliveryRank.position_of(ActiveDelivery)
    assert SelectedConsumedProgress(100).rank() == DeliveryRank.position_of(ActiveDelivery)


COLLEAGUE, WORKER = RecordedThreadIncarnation("colleague", 1.0), RecordedThreadIncarnation("worker", 2.0)
ORDERS = (OrderId.mint(),)
UI_ONLY_FIELDS = {"root", "request_id", "previous", "owner", "identity"}
SHARED_UI_READS = [
    TeammateRead(frame_owner=COLLEAGUE, unit=WORKER, orders=ORDERS),
    TranscriptPageRead(thread="worker", position=EarlierThan(NativeTranscriptCursor("session.jsonl", 7, file=MissingFile())),
                       page_source=RecordedPageSource(HistorySourceKey("archive"))),
]


@pytest.mark.parametrize("operation", SHARED_UI_READS, ids=["teammate", "transcript_page"])
def test_shared_ui_reads_are_root_free_family_records_in_one_generic_envelope(operation):
    encoded = FieldCodec.encode(operation)
    assert not UI_ONLY_FIELDS & set(encoded)
    assert FieldCodec.decode(UiRead, encoded) == operation
    request = ReadOperation("root", operation, request_id=4)
    assert FieldCodec.decode(ReadOperation, FieldCodec.encode(request)) == request
    transported = pickle.loads(pickle.dumps(request))
    assert transported == request and hash(transported) == hash(request)
    assert {transported: "held"}[request] == "held" and len({request, transported}) == 1


@pytest.mark.parametrize("operation", (OwnerGoalRead(thread="worker"), InputDeliveryRead(thread="worker")))
def test_owner_scoped_reads_are_root_free_records_in_their_envelope(operation: OwnerScopedRead) -> None:
    encoded = FieldCodec.encode(operation)
    assert not UI_ONLY_FIELDS & set(encoded)
    assert FieldCodec.decode(OwnerScopedRead, encoded) == operation
    request = ReadOwnerOperation("root", WORKER, operation=operation, request_id=4)
    assert FieldCodec.decode(ReadOwnerOperation, FieldCodec.encode(request)) == request
    transported = pickle.loads(pickle.dumps(request))
    assert transported == request and hash(transported) == hash(request)


def test_witnesses_cursors_and_archive_each_distinguish_a_shared_read_request():
    teammate_read = ReadOperation("root", SHARED_UI_READS[0], request_id=4)
    page = ReadOperation("root", SHARED_UI_READS[1], request_id=4)
    distinct = (
        replace(teammate_read, root="other-root"),
        replace(teammate_read, request_id=5),
        replace(teammate_read, operation=replace(SHARED_UI_READS[0], unit=RecordedThreadIncarnation("worker", 3.0))),
        replace(teammate_read, operation=replace(SHARED_UI_READS[0], orders=(OrderId.mint(),))),
        page,
        replace(page, operation=replace(SHARED_UI_READS[1], position=EarlierThan(NativeTranscriptCursor("session.jsonl", 8, file=MissingFile())))),
        replace(page, operation=replace(SHARED_UI_READS[1], page_source=RecordedPageSource("other-archive"))),
    )
    assert len({teammate_read, *distinct}) == 1 + len(distinct)


def test_one_shared_read_in_two_root_envelopes_keeps_root_validation(comms):
    operation = SHARED_UI_READS[1]
    ours = ReadOperation(str(comms.root), operation)
    foreign = ReadOperation(str(comms.root.parent / "another-wire"), operation)
    assert ours.operation is foreign.operation and ours != foreign
    ours.require_root(comms)
    with pytest.raises(ReloadRequired):
        foreign.require_root(comms)


def test_the_three_public_reads_are_cataloged_once_with_their_original_results():
    sys.path.insert(0, str(Path(__file__).parent))

    tools = ToolRequest.members_with(ToolRequest)
    native = NativeRequestTools.members_with(NativeRequestTools)
    results = {TeammateRead: TeammateDetail, OwnerGoalRead: GoalSnapshotReply,
               TranscriptPageRead: TranscriptSnapshotUpdate}
    for operation, result in results.items():
        served = ToolRequest.member_named(operation.declared_name)
        assert issubclass(served, operation) and served.result_type() is result
        assert tools.count(served) == 1 and native.count(served) == 1
    assert {read.declared_name for read in ToolRequest.members_with(UiRead)} == {
        TeammateRead.declared_name, TranscriptPageRead.declared_name}
    assert ToolRequest.members_with(OwnerScopedRead) == NativeRequestTools.members_with(OwnerScopedRead)
    lifecycle = (ReadOperation, ReadOwnerOperation, ReadNotifications, ReadTranscript,
                 SubscribeTranscript, ReadTranscriptUpdate, RefreshTranscript)
    assert not set(lifecycle) & {*tools, *native}
