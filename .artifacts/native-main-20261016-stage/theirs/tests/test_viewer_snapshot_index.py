"""A fresh sidebar view reuses validated bus history rather than decoding it."""

import asyncio
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from multiprocessing import Pipe
from unittest.mock import patch

import pytest

from agent_comms.bus_display_index import BusDisplayIndex
from agent_comms.bus_route_counts import BusRouteCounts
from agent_comms.comms import wire
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import CoordinationReadUnavailable, StaleRevision
from agent_comms.errors import RelationViolationError
from agent_comms.messages import InfoKind, Message
from agent_comms.threads import Thread
from agent_comms.wire_log import OpenedWireSnapshot
from agent_comms.ui_model.observation import (
    ObservationService,
    ObserveSidebar,
    SidebarObserved,
    SidebarReading,
)
from agent_comms.ui_model.unread import IndexingUnread


def test_display_metrics_encode_one_viewer_read_set_for_all_channels(tmp_path):
    from agent_comms.bus_display_index import DisplayMetricScope
    from agent_comms.field_codec import FieldCodec
    from agent_comms.read_basis import ChannelDisplayScope

    scopes = tuple(ChannelDisplayScope(f"#{n}", frozenset({f"#{n}"})) for n in range(48))
    seen = frozenset(range(10_000))
    encoded = FieldCodec.encode(DisplayMetricScope(scopes, frozenset({"viewer"}), seen))
    assert set(encoded) == {"scopes", "viewer_names", "seen_sequences"}
    assert frozenset(encoded["seen_sequences"]) == seen
    assert all("seen_sequences" not in scope for scope in encoded["scopes"])
    index = BusDisplayIndex(tmp_path / "bus.jsonl", "viewer")
    assert index.path.name.startswith(f"bus_display_{index.record_type.schema}_")


def test_display_rebase_combines_changed_reads_and_changed_channel_inclusion(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("bob", frozenset({"team", "extra"}), str(tmp_path / "bob")))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    first = comms.messaging.send_message("bob", "#team", "first")
    comms.messaging.send_message("bob", "#team", "second")
    comms.messaging.send_message("bob", viewer, "direct")
    before = comms.views.viewer_snapshot(str(tmp_path))
    comms.bus.reads.mark_displayed(
        viewer,
        comms.bus.reads.capture(viewer, (first,), comms.registry.snapshot(), comms.bus.log.path),
    )
    comms.channels.set_channel_any_mode("#extra", True)
    after = wire(tmp_path).views.viewer_snapshot(str(tmp_path))
    assert after.channel_unread["#team"] == 1
    assert after.channel_unread["#extra"] == 2
    def clock(snapshot):
        return next(v.last_activity for v in snapshot.channels if v.channel.name == "#team")

    assert clock(after) == clock(before)


@pytest.mark.parametrize("indexed", [True, False])
def test_viewer_delivery_and_sent_share_original_cut(tmp_path, indexed):
    """Later append/ACK/rename cannot broaden an acquired viewer snapshot."""
    from dataclasses import replace

    comms = wire(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path))
    comms.registry.register(replace(viewer, tags=frozenset({"team"})))
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    direct = comms.messaging.send_message("bob", viewer.name, "direct")
    channel = comms.messaging.send_message("bob", "#team", "channel")
    seen = comms.bus.reads.capture(viewer.name, (direct,), comms.registry.snapshot(),
                                 comms.bus.log.path)
    comms.bus.reads.mark_displayed(viewer.name, seen)
    index_path = BusRouteCounts(comms.bus.log.path).path
    if not indexed:
        # A refused store is not permission to introduce a full-source reader.
        index_path.mkdir()
        with pytest.raises(sqlite3.OperationalError) as unavailable:
            with closing(CoordinationStore.read_snapshot(index_path)):
                pass
    with comms.views.presentation.snapshot(viewer=viewer.name) as (basis, source):
        before = {"#team": 1}
        sent = comms.views.last_sent_timestamps(source=source, registry=basis.registry)
        if indexed:
            assert comms.bus.pending_counts_opened(source, basis) == before
        else:
            with pytest.raises(sqlite3.OperationalError) as refused:
                comms.bus.pending_counts_opened(source, basis)
            assert refused.value.sqlite_errorcode == unavailable.value.sqlite_errorcode
        comms.bus.reads.mark_displayed(viewer.name, comms.bus.reads.capture(
            viewer.name, (channel,), comms.registry.snapshot(), comms.bus.log.path))
        comms.messaging.send_message("bob", viewer.name, "later direct")
        comms.registry.rename("bob", "renamed-bob")
        if not indexed:
            with pytest.raises(sqlite3.OperationalError) as refused:
                comms.bus.pending_counts_opened(source, basis)
            assert refused.value.sqlite_errorcode == unavailable.value.sqlite_errorcode
            index_path.rmdir()
        # Repair the derived resource, not the captured source or read ledger.
        assert comms.bus.pending_counts_opened(source, basis) == before
        assert comms.views.last_sent_timestamps(source=source, registry=basis.registry) == sent
        assert "bob" in sent and "renamed-bob" not in sent
    latest = comms.views.viewer_snapshot(str(tmp_path))
    assert latest.unread == {"renamed-bob": 1}
    assert "renamed-bob" in latest.last_sent and "bob" not in latest.last_sent


@pytest.mark.asyncio
async def test_sidebar_roster_precedes_rewritten_bus_projection_rebuild(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path))
    comms.registry.register(viewer)
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    comms.messaging.send_message("bob", viewer.name, "direct")
    comms.messaging.send_message("bob", "#team", "channel")
    expected = comms.views.viewer_snapshot(str(tmp_path))

    bus_path = comms.bus.log.path
    original = bus_path.read_bytes()
    modified = bus_path.stat().st_mtime_ns
    bus_path.write_bytes(original)
    os.utime(bus_path, ns=(modified + 1_000_000_000, modified + 1_000_000_000))
    BusRouteCounts(bus_path).path.unlink()

    def projection_bytes():
        paths = [
            comms.bus.log.path.with_name("bus_activity_latest.json"),
            BusRouteCounts(comms.bus.log.path).path,
            *comms.bus.log.path.parent.glob("bus_display_*.json"),
        ]
        return {path.name: path.read_bytes() for path in paths if path.exists()}

    before = projection_bytes()

    receiver, connection = Pipe()
    service = ObservationService(comms, connection)
    request = ObserveSidebar(str(tmp_path), True, False)
    request.receive(service)
    results = []
    original_snapshot = comms.views.viewer_snapshot
    from agent_comms.bus_route_counts import RouteIndexRebuilding
    from agent_comms.ui_model.observation import ReadAnswered, ReadRequest

    class ForegroundRead(ReadRequest):
        def read(self, service_comms):
            return service_comms.registry.require("bob").name

    monkeypatch.setattr(BusRouteCounts, "rebuild_bytes", 1)
    attempts = []

    def complete(*args, **kwargs):
        # The service can finish both stages in one observation. Inspect the
        # actual pipe at the read boundary, not a presumed number of ticks.
        while receiver.poll():
            results.append(receiver.recv())
        first = next(value for value in results if isinstance(value, SidebarObserved))
        assert first.snapshot.unread_pending >= {"bob", "#team"}
        assert isinstance(first.derived[0]["#team"].unread, IndexingUnread)
        if not attempts:
            assert projection_bytes() == before
        else:
            # The same observer serves actual foreground work between the two
            # admitted index units, with no append, expiry tick or new request.
            assert any(isinstance(value, ReadAnswered) and value.request_id == 9976
                       and value.value == "bob" for value in results)
        attempts.append(True)
        try:
            return original_snapshot(*args, **kwargs)
        except RouteIndexRebuilding:
            ForegroundRead(str(comms.root), request_id=9976).receive(service)
            raise

    try:
        with patch.object(comms.views, "viewer_snapshot", side_effect=complete):
            await service.observe(set())
        while receiver.poll():
            results.append(receiver.recv())
        completed = [value for value in results if isinstance(value, SidebarObserved)][-1]
        assert completed.snapshot == expected
        assert completed.snapshot.unread_pending == frozenset()
        assert len(attempts) == 2
        assert service.observations[SidebarReading].request is request
    finally:
        connection.close()
        receiver.close()
        comms.views.transcript_reads.close()


@pytest.mark.asyncio
async def test_sidebar_does_not_spin_when_route_preparation_is_owned_by_another_writer(
    tmp_path, monkeypatch,
):
    from agent_comms.store_files import _store_lock

    comms = wire(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    comms.registry.declare(Thread("sender", frozenset(), str(tmp_path)))
    for body in ("first", "second"):
        comms.messaging.send_message("sender", viewer, body)
    index = BusRouteCounts(comms.bus.log.path)
    index.prepare()
    monkeypatch.setattr(BusRouteCounts, "rebuild_bytes", 1)
    receiver, connection = Pipe()
    service = ObservationService(comms, connection)
    request = ObserveSidebar(str(tmp_path), True, False)
    request.receive(service)
    try:
        with _store_lock(index.path):
            await asyncio.wait_for(service.observe(set()), timeout=2)
        answers = []
        while receiver.poll():
            answers.append(receiver.recv())
        initial = [answer for answer in answers if isinstance(answer, SidebarObserved)]
        assert len(initial) == 1 and initial[0].snapshot.unread_pending
        await service.observe(set())
        while receiver.poll():
            answers.append(receiver.recv())
        completed = [answer for answer in answers if isinstance(answer, SidebarObserved)][-1]
        assert completed.snapshot.unread == {"sender": 2}
        assert not completed.snapshot.unread_pending
        assert service.observations[SidebarReading].request is request
    finally:
        connection.close()
        receiver.close()
        comms.views.transcript_reads.close()


def test_viewer_snapshot_acquires_one_bus_cut(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    viewer = comms.messaging.user_identity(str(tmp_path))
    comms.messaging.send_message("bob", viewer.name, "direct")
    comms.messaging.send_message("bob", "#team", "channel")
    original = comms.bus.log._opened_wire_snapshot
    opened = []

    @contextmanager
    def observed(*args, **kwargs):
        with original(*args, **kwargs) as source:
            opened.append(source)
            yield source

    monkeypatch.setattr(comms.bus.log, "_opened_wire_snapshot", observed)
    snapshot = comms.views.viewer_snapshot(str(tmp_path))
    assert snapshot.unread == {"bob": 1}
    assert snapshot.channel_unread["#team"] == 1
    assert snapshot.last_sent["bob"] > 0
    assert len(opened) == 1 and opened[0].stream.closed


@pytest.mark.asyncio
async def test_sidebar_retries_an_activity_source_change(tmp_path, monkeypatch):
    """An actual stale-read outcome preserves the same observer and last answer."""
    comms = wire(tmp_path)
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    comms.messaging.send("bob", "#team", "first")

    receiver, connection = Pipe()
    service = ObservationService(comms, connection)
    request = ObserveSidebar(str(tmp_path), True, False)
    request.receive(service)
    try:
        await service.observe(set())
        results = []
        while receiver.poll():
            results.append(receiver.recv())
        published = service.observations[SidebarReading].published
        assert published.snapshot.channel_unread["#team"] == 1
        observer = service.observations[SidebarReading]
        original = comms.views.presentation.display_view_metrics
        comms.messaging.send("bob", "#team", "second")
        preserved = comms.bus.log.path.read_bytes()

        def change_source(*args, **kwargs):
            original(*args, **kwargs)
            # Append/mtime changes alone do not revoke a retained descriptor.
            # The activity reader's named stale outcome is what defers work.
            raise StaleRevision("Activity source changed before its captured read")

        monkeypatch.setattr(comms.views.presentation, "display_view_metrics", change_source)
        await service.observe(set())
        while receiver.poll():
            result = receiver.recv()
            assert not isinstance(result, SidebarObserved)
        assert observer.published is published
        assert service.observations[SidebarReading] is observer
        assert observer.request is request

        monkeypatch.setattr(comms.views.presentation, "display_view_metrics", original)
        # No new append is needed to retry the deferred revision.
        await service.observe(set())
        recovered = []
        while receiver.poll():
            recovered.append(receiver.recv())
        latest = [r for r in recovered if isinstance(r, SidebarObserved)][-1]
        assert latest.snapshot.channel_unread["#team"] == 2
        assert service.observations[SidebarReading] is observer
        assert observer.request is request
        assert comms.bus.log.path.read_bytes() == preserved
    finally:
        connection.close()
        receiver.close()
        comms.views.transcript_reads.close()


def test_reopened_viewer_snapshot_decodes_only_appended_rows(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    for number in range(100):
        comms.messaging.send("bob", "#team", f"message {number}")
    expected = comms.views.viewer_snapshot(str(tmp_path))
    assert expected.channel_unread["#team"] == 100

    fresh = wire(tmp_path)
    with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
        assert fresh.views.viewer_snapshot(str(tmp_path)) == expected
        assert decode.call_count == 0

    comms.messaging.send("bob", "#team", "one new message")
    with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
        latest = fresh.views.viewer_snapshot(str(tmp_path))
        assert latest.channel_unread["#team"] == 101
        # Every decode belongs to the appended row; retained history is not replayed.
        assert decode.call_count > 0
        assert all(call.args[0]["text"] == "one new message" for call in decode.call_args_list)

    comms.channels.set_channel_any_mode("#team", True)
    assert (fresh.views.viewer_snapshot(str(tmp_path))
            == wire(tmp_path).views.viewer_snapshot(str(tmp_path)))


def test_display_checkpoint_rebuilds_but_bus_replacement_is_refused(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    comms.messaging.send("bob", "#team", "first")
    comms.messaging.send("bob", "#team", "second")
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 2

    checkpoint = BusDisplayIndex(
        comms.bus.log.path, comms.messaging.user_identity(str(tmp_path)).name
    ).path
    cached = json.loads(checkpoint.read_text())
    cached["counts"]["#team"] = 0
    checkpoint.write_text(json.dumps(cached))
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 2

    bus_path = comms.bus.log.path
    replacement = bus_path.with_name("replacement.jsonl")
    replacement.write_bytes(bus_path.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, bus_path)
    with pytest.raises(RelationViolationError, match="checkpoint root/inode/size changed"):
        wire(tmp_path).views.viewer_snapshot(str(tmp_path))


def test_display_read_progress_decodes_only_changed_sparse_rows(tmp_path):
    """Real page coverage and read receipts keep clocks independent of unread."""
    comms = wire(tmp_path)
    comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    messages = [comms.messaging.send_message("bob", "#team", f"message {n}")
                for n in range(100)]
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    before = comms.views.viewer_snapshot(str(tmp_path))
    # The real page owner builds the already-existing disposable offset index.
    page = comms.views.presentation.channel_page("#team", viewer=viewer)
    assert len(page.messages) == 100
    comms.bus.reads.mark_displayed(
        viewer, comms.bus.reads.capture(viewer, (messages[1], messages[90]),
                                       comms.registry.snapshot(), comms.bus.log.path),
    )
    with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
        after = wire(tmp_path).views.viewer_snapshot(str(tmp_path))
        assert after.channel_unread["#team"] == 98
        assert decode.call_count == 2
        assert {call.args[0]["seq"] for call in decode.call_args_list} == {
            messages[1].seq, messages[90].seq,
        }
    assert after.channels == before.channels
    # A new row still contributes once, after the rebased old prefix.
    comms.messaging.send_message("bob", "#team", "appended")
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 99


def test_display_read_changes_prepare_missing_index_then_query_sparse_rows(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    messages = [comms.messaging.send_message("bob", "#team", f"message {n}")
                for n in range(10)]
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    before = comms.views.viewer_snapshot(str(tmp_path))
    comms.bus.reads.mark_displayed(
        viewer, comms.bus.reads.capture(viewer, (messages[3],),
                                       comms.registry.snapshot(), comms.bus.log.path),
    )
    index_path = comms.bus.log.path.with_name("bus_page_index.sqlite3")
    assert not index_path.exists()

    read_appended = OpenedWireSnapshot.public_records

    def no_full_scan(source, offset=0, *, before_offset=None):
        assert offset > 0, "Missing page index must be prepared, not bypassed"
        return read_appended(source, offset, before_offset=before_offset)

    with (
        patch.object(Message, "from_wire", wraps=Message.from_wire) as decode,
        patch.object(OpenedWireSnapshot, "public_records", no_full_scan),
    ):
        after = wire(tmp_path).views.viewer_snapshot(str(tmp_path))
        assert after.channel_unread["#team"] == 9
        assert decode.call_count == len(messages) + 1  # Build once, then select one row.
        assert decode.call_args_list[-1].args[0]["seq"] == messages[3].seq
    assert after.channels == before.channels
    assert index_path.exists()


def test_display_does_not_keep_read_counts_across_viewer_incarnations(tmp_path):
    from dataclasses import replace

    comms = wire(tmp_path)
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    message = comms.messaging.send_message("bob", "#team", "retained")
    viewer = comms.messaging.user_identity(str(tmp_path))
    comms.views.presentation.channel_page("#team", viewer=viewer.name)
    comms.views.viewer_snapshot(str(tmp_path))
    comms.bus.reads.mark_displayed(
        viewer.name, comms.bus.reads.capture(viewer.name, (message,),
                                            comms.registry.snapshot(), comms.bus.log.path),
    )
    read = wire(tmp_path).views.viewer_snapshot(str(tmp_path))
    assert read.channel_unread["#team"] == 0
    # Metadata updates preserve identity; removal and a new declaration do not.
    comms.registry.remove(viewer.name)
    comms.registry.register(replace(viewer, created_at=viewer.created_at + 1))
    returned = wire(tmp_path).views.viewer_snapshot(str(tmp_path))
    assert returned.channel_unread["#team"] == 1
    assert returned.channels == read.channels


def test_append_between_revision_and_opened_bus_boundary_uses_captured_records(
    tmp_path, monkeypatch
):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    comms.messaging.send("bob", "#team", "first")
    appended = []
    original = comms.bus.log._opened_wire_snapshot

    @contextmanager
    def append_before_open(*args, **kwargs):
        # _display_snapshot already holds the wire lock at this boundary.
        appended.append(comms.bus.publisher.publish_ordinary(
            Message("bob", "#team", "raced append", InfoKind)
        ))
        with original(*args, **kwargs) as boundary:
            yield boundary

    monkeypatch.setattr(comms.bus.log, '_opened_wire_snapshot', append_before_open)
    snapshot = comms.views.viewer_snapshot(str(tmp_path))
    team = next(view for view in snapshot.channels if view.channel.name == "#team")
    assert snapshot.channel_unread["#team"] == 2
    assert team.last_activity == appended[0].timestamp


@pytest.mark.asyncio
async def test_reopened_human_pending_routes_keep_sparse_reads_aliases_and_recover(tmp_path):
    from collections import Counter

    from agent_comms.bus_route_counts import BusRouteCounts

    comms = wire(tmp_path)
    comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("bob", frozenset(), str(tmp_path / "bob")))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    messages = [
        comms.messaging.send_message("bob", viewer if number % 2 else "#team", f"message {number}")
        for number in range(6)
    ]
    comms.messaging.send_user_message("bob", "own outgoing message", worktree=str(tmp_path))
    comms.views.viewer_snapshot(str(tmp_path))
    comms.bus.reads.mark_displayed(
        viewer,
        comms.bus.reads.capture(
            viewer, (messages[1], messages[4]), comms.registry.snapshot(), comms.bus.log.path
        ),
    )
    assert wire(tmp_path).bus.pending_counts(viewer) == {"bob": 2}
    comms.registry.rename("bob", "renamed-bob")
    from agent_comms.routing import DeliveryScope

    delivery = DeliveryScope.capture(
        viewer, comms.registry.snapshot(), comms.channels.catalog.read()
    )
    expected = Counter(
        delivery.conversation(message.sender, message.target) for message in comms.bus.inbox(viewer)
    )
    # Rename preserves sparse read evidence through the canonical thread alias.
    # The untagged human inbox still excludes channel rows.
    assert expected == {"renamed-bob": 2}
    with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
        assert wire(tmp_path).bus.pending_counts(viewer) == expected
        assert decode.call_count == 0
    ledger = comms.bus.reads.read()
    receiver, connection = Pipe()
    service = ObservationService(comms, connection)
    request = ObserveSidebar(str(tmp_path), True, False)
    request.receive(service)
    try:
        await service.observe(set())
        while receiver.poll():
            receiver.recv()
        observer = service.observations[SidebarReading]
        published = observer.published
        assert published.snapshot.unread == expected
        with closing(CoordinationStore.writer(BusRouteCounts(comms.bus.log.path).path)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            comms.messaging.send_message("renamed-bob", viewer, "during index contention")
            # Use the real SQLite busy result, not a wording-only DatabaseError.
            with patch.object(CoordinationStore, "writer_timeout", 0):
                with pytest.raises(CoordinationReadUnavailable) as busy:
                    wire(tmp_path).bus.pending_counts(viewer)
                assert busy.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_BUSY
                await service.observe(set())
            writer.rollback()
        while receiver.poll():
            result = receiver.recv()
            assert not isinstance(result, SidebarObserved)
        assert service.observations[SidebarReading] is observer
        assert observer.published is published
        assert observer.request is request
        await service.observe(set())
        recovered = []
        while receiver.poll():
            recovered.append(receiver.recv())
        latest = [r for r in recovered if isinstance(r, SidebarObserved)][-1]
        assert latest.snapshot.unread == {"renamed-bob": 3}
        assert wire(tmp_path).bus.pending_counts(viewer) == {"renamed-bob": 3}
        assert service.observations[SidebarReading] is observer
        assert observer.request is request
        assert comms.bus.reads.read() == ledger
    finally:
        connection.close()
        receiver.close()
        comms.views.transcript_reads.close()


def test_certified_display_retains_original_cut_and_persists_changed_scope(tmp_path):
    from agent_comms.wire_log import CertifiedOpenedWireSnapshot
    from test_private_bus_checkpoint import _root
    from test_turn_context_wire import manifest

    comms, _ = _root(tmp_path)
    first = comms.messaging.send_initial_cohort('sender', '#team', '@Alice first')
    comms.bus.log.record_context(manifest(comms.registry.require('Alice')))
    with comms.views.presentation.snapshot(viewer='Alice') as (_, source):
        assert isinstance(source, CertifiedOpenedWireSnapshot)
        # Original lock has ended. A genuine later publication cannot broaden
        # this descriptor's cut or make the captured observation visible.
        second = comms.messaging.send_initial_cohort('sender', '#team', '@Alice next')
        assert [message.message_id for message, _ in source.public_records()] == [first.message_id]
    assert source.stream.closed
    assert [message.message_id for message in comms.bus.log.full_history()] == [
        first.message_id, second.message_id
    ]

    comms.views.viewer_snapshot(str(tmp_path))
    comms.channels.set_channel_any_mode('#team', True)
    comms.views.viewer_snapshot(str(tmp_path))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    checkpoint = BusDisplayIndex(comms.bus.log.path, viewer).path
    current = json.loads(checkpoint.read_text())
    assert next(scope for scope in current['semantics']['scopes']
                if scope['channel'] == '#team')['any_mode'] is True


def test_uncertified_opened_cut_still_rejects_unknown_observation(tmp_path):
    from agent_comms.turn_context import RecordedContextManifest
    from agent_comms.wire_log import OpenedWireSnapshot, WireLog
    from agent_comms.wire_record import (
        ObservationWireRecord,
        RecordedContextManifestWireObservation,
    )
    from test_turn_context_wire import manifest

    tmp_path.chmod(0o700)
    log = WireLog(tmp_path / 'bus.jsonl')
    from agent_comms.wire_metadata import WireMetadata
    log.write_metadata_unlocked(WireMetadata(writer_protocol_version=1, wire_root_id='0' * 32))
    owner = Thread('author', frozenset(), str(tmp_path), created_at=17003.0)
    recorded, _entries = RecordedContextManifest.capture(manifest(owner))
    row = ObservationWireRecord(RecordedContextManifestWireObservation(recorded))
    log.path.write_text(json.dumps(row.to_wire()) + '\n')
    log.path.chmod(0o600)
    with log._opened_wire_snapshot(need_sequence=False) as source:
        assert type(source) is OpenedWireSnapshot
        assert list(source.public_records()) == []
    log.path.write_text(json.dumps({'observation': {'kind': 'unknown'}}) + '\n')
    with log._opened_wire_snapshot(need_sequence=False) as source:
        with pytest.raises((ValueError, TypeError, KeyError)):
            list(source.public_records())
        with pytest.raises((ValueError, TypeError, KeyError)):
            list(source.public_records_for(frozenset({1}), before_offset=source.boundary))


def test_reserved_sequence_preserves_committed_read_cut_without_admission(tmp_path):
    from agent_comms.wire_log import CertifiedOpenedWireSnapshot
    from test_private_bus_checkpoint import _root

    comms, _ = _root(tmp_path)
    first = comms.messaging.send_initial_cohort('sender', '#team', '@Alice first')
    # This is the original durable state between reservation and append.
    # No second input or fabricated successful append resolves that uncertainty.
    with comms.bus.log.locked():
        marker = comms.bus.log._private_marker_unlocked()
        marker.last_seq += 1
        comms.bus.log.write_metadata_unlocked(marker)
    with comms.bus.log._opened_wire_snapshot(need_sequence=False) as source:
        assert isinstance(source, CertifiedOpenedWireSnapshot)
        assert [message.message_id for message, _ in source.public_records()] == [first.message_id]
    with comms.bus.snapshot(viewer='Alice') as (basis, source):
        assert source.through == first.seq < source.metadata.last_seq
        assert comms.bus.pending_counts_opened(source, basis) == {'#team': 1}
    with pytest.raises(RelationViolationError, match='differs from its durable marker'):
        with comms.bus.log.certified_read() as source:
            source.require_current()
