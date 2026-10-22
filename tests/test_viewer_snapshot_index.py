"""A fresh sidebar view reuses validated bus history rather than decoding it."""

from agent_comms.context_store import UNREAD_CONTEXT
from agent_comms.wire_log import WireLog

from conftest import publish_initial
from agent_comms.read_basis import NamedViewer
import json
import os
import sqlite3
from contextlib import closing, contextmanager

import pytest

from agent_comms.bus_display_index import BusDisplayIndex
from agent_comms.bus_route_counts import BusRouteCounts
from agent_comms.candidate_maintenance import drain_candidate_catchup
from agent_comms.comms import wire
from agent_comms.locked_store import SQLiteFile
from agent_comms.errors import RelationViolationError, StoredHistoryUnreadable
from agent_comms.wire_log import CurrentSnapshotOpening, OpenedWireSnapshot
from agent_comms.ui_model.unread import ExactUnread


def positive_unread(snapshot):
    """Old sparse-count assertions inspect only positive entries of exact typed rows."""
    assert all(isinstance(value, ExactUnread) for value in snapshot.unread.values())
    return {name: value.count for name, value in snapshot.unread.items() if value.count}


@contextmanager
def refused_route_index(comms):
    """Make a reader's derived store unavailable while no publisher is using it."""
    drain_candidate_catchup(comms.root)
    path = BusRouteCounts(comms.bus.log.path).path
    for member in SQLiteFile.files(path):
        member.unlink(missing_ok=True)
    path.mkdir()
    try:
        yield path
    finally:
        path.rmdir()


def test_display_metrics_store_only_append_facts(tmp_path):
    from agent_comms.bus_display_index import DisplaySnapshot
    from agent_comms.field_codec import FieldCodec
    from agent_comms.read_basis import ChannelDisplayScope

    scopes = tuple(ChannelDisplayScope(f"#{n}", frozenset({f"#{n}"})) for n in range(48))
    encoded = FieldCodec.encode(DisplaySnapshot.empty(scopes))
    assert set(encoded) == {"scopes", "activity"}
    index = BusDisplayIndex(tmp_path / "bus.jsonl", "viewer")
    assert index.path.name.startswith(f"bus_display_{BusDisplayIndex.record_type.schema}_")


def test_display_rebase_combines_changed_reads_and_changed_channel_inclusion(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team', 'extra'}), str(tmp_path / 'bob')))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    first = comms.messaging.send("bob", "#team", "first").message
    comms.messaging.send("bob", "#team", "second").message
    comms.messaging.send("bob", viewer, "direct").message
    before = comms.views.viewer_snapshot(str(tmp_path))
    document = comms.bus.reads.read()
    with WireLog(comms.bus.log.path).opened_wire_snapshot() as opened:
        displayed = opened.query_page_index(lambda index: comms.bus.reads.capture_indexed(
            viewer, (first,), comms.registry.snapshot(), opened, index, document=document))
    comms.bus.reads.mark_displayed(viewer, displayed)
    comms.channels.set_channel_any_mode("#extra", True)
    after = wire(tmp_path).views.viewer_snapshot(str(tmp_path))
    assert after.channel_unread["#team"].count == 1
    assert after.channel_unread["#extra"].count == 2
    def clock(snapshot):
        return snapshot.channels["#team"].last_activity

    assert clock(after) == clock(before)


@pytest.mark.parametrize("indexed", [True, False])
def test_viewer_delivery_and_sent_share_original_cut(tmp_path, indexed):
    """Later append/ACK/rename cannot broaden an acquired viewer snapshot."""
    from dataclasses import replace

    comms = wire(tmp_path)
    viewer = comms.messaging.user_identity(str(tmp_path))
    comms.registry.register(replace(viewer, tags=frozenset({"team"})))
    comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team'}), str(tmp_path / 'bob')))
    direct = comms.messaging.send("bob", viewer.name, "direct").message
    channel = comms.messaging.send("bob", "#team", "channel").message
    document = comms.bus.reads.read()
    with WireLog(comms.bus.log.path).opened_wire_snapshot() as opened:
        seen = opened.query_page_index(lambda index: comms.bus.reads.capture_indexed(
            viewer.name, (direct,), comms.registry.snapshot(), opened, index, document=document))
    comms.bus.reads.mark_displayed(viewer.name, seen)
    with comms.views.presentation.snapshot(viewer=NamedViewer(viewer.name)) as (basis, source):
        before = {"#team": 1}
        sent = comms.views.last_sent_timestamps(comms.bus.activity_index.snapshot(source).sent.items(), registry=basis.registry)
        if indexed:
            assert comms.bus.pending_counts_opened(source, basis) == before
        else:
            # A refused store is not permission to introduce a full-source reader.
            with refused_route_index(comms) as index_path:
                with pytest.raises(sqlite3.OperationalError) as unavailable:
                    with closing(SQLiteFile.read_snapshot(index_path)):
                        pass
                with pytest.raises(sqlite3.OperationalError) as refused:
                    comms.bus.pending_counts_opened(source, basis)
                assert refused.value.sqlite_errorcode == unavailable.value.sqlite_errorcode
        document = comms.bus.reads.read()
        with WireLog(comms.bus.log.path).opened_wire_snapshot() as opened:
            displayed = opened.query_page_index(lambda index: comms.bus.reads.capture_indexed(
                viewer.name, (channel,), comms.registry.snapshot(), opened, index, document=document))
        comms.bus.reads.mark_displayed(viewer.name, displayed)
        comms.messaging.send("bob", viewer.name, "later direct")
        comms.registry.rename("bob", "renamed-bob")
        if not indexed:
            with refused_route_index(comms):
                with pytest.raises(sqlite3.OperationalError) as refused:
                    comms.bus.pending_counts_opened(source, basis)
                assert refused.value.sqlite_errorcode == unavailable.value.sqlite_errorcode
        # Repair the derived resource, not the captured source or read ledger.
        assert comms.bus.pending_counts_opened(source, basis) == before
        assert comms.views.last_sent_timestamps(comms.bus.activity_index.snapshot(source).sent.items(), registry=basis.registry) == sent
        assert "bob" in sent and "renamed-bob" not in sent
    latest = comms.views.viewer_snapshot(str(tmp_path))
    assert positive_unread(latest) == {"renamed-bob": 1}
    assert "renamed-bob" in latest.last_sent and "bob" not in latest.last_sent


def test_route_index_write_is_a_sidebar_revision(tmp_path):
    from agent_comms.presentation import WireRevision
    from agent_comms.revision_views import SidebarView

    comms = wire(tmp_path)
    index = BusRouteCounts(comms.bus.log.path)
    index.prepare()
    before = WireRevision.capture(comms.views, expiry_tick=0)
    with index.writing() as writer:
        writer.execute('CREATE TABLE p730_revision_probe (id INTEGER)')
        writer.execute('INSERT INTO p730_revision_probe VALUES (1)')
    after = WireRevision.capture(comms.views, expiry_tick=0)
    assert before.bus_log == after.bus_log
    assert after.changed_since(SidebarView(), before)


def test_display_index_rebuilds_after_bus_replacement(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('alice', frozenset({'team'}), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team'}), str(tmp_path / 'bob')))
    comms.messaging.send("bob", "#team", "first").message.message_id
    comms.messaging.send("bob", "#team", "second").message.message_id
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 2

    checkpoint = BusDisplayIndex(
        comms.bus.log.path, comms.messaging.user_identity(str(tmp_path)).name
    ).path
    cached = json.loads(checkpoint.read_text())
    cached["activity"]["#team"]["last_message"] = 0
    checkpoint.write_text(json.dumps(cached))
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 2

    bus_path = comms.bus.log.path
    two = bus_path.read_bytes()
    comms.messaging.send("bob", "#team", "third").message.message_id
    three = bus_path.read_bytes()
    replacement = bus_path.with_name("replacement.jsonl")
    replacement.write_bytes(two)
    replacement.chmod(0o600)
    os.replace(replacement, bus_path)
    with pytest.raises(RelationViolationError):
        wire(tmp_path).views.viewer_snapshot(str(tmp_path))
    replacement.write_bytes(three)
    replacement.chmod(0o600)
    os.replace(replacement, bus_path)
    assert wire(tmp_path).views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 3


def test_display_high_water_and_sparse_reads_leave_activity_unchanged(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team'}), str(tmp_path / 'bob')))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    messages = [comms.messaging.send("bob", "#team", f"message {n}").message for n in range(8)]
    comms.messaging.send_user_message("#team", "my message", worktree=str(tmp_path))
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 8
    saved = BusDisplayIndex(comms.bus.log.path, viewer).path
    stored = saved.read_bytes()
    document = comms.bus.reads.read()
    with WireLog(comms.bus.log.path).opened_wire_snapshot() as opened:
        displayed = opened.query_page_index(lambda index: comms.bus.reads.capture_indexed(
            viewer, (*messages[:4], messages[6]), comms.registry.snapshot(), opened, index, document=document))
    comms.bus.reads.mark_displayed(viewer, displayed)
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"].count == 3
    assert saved.read_bytes() == stored


def test_display_does_not_keep_read_counts_across_viewer_incarnations(tmp_path):
    from dataclasses import replace

    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team'}), str(tmp_path / 'bob')))
    message = comms.messaging.send("bob", "#team", "retained").message
    viewer = comms.messaging.user_identity(str(tmp_path))
    comms.views.presentation.channel_page("#team", viewer=NamedViewer(viewer.name))
    comms.views.viewer_snapshot(str(tmp_path))
    document = comms.bus.reads.read()
    with WireLog(comms.bus.log.path).opened_wire_snapshot() as opened:
        displayed = opened.query_page_index(lambda index: comms.bus.reads.capture_indexed(
            viewer.name, (message,), comms.registry.snapshot(), opened, index, document=document))
    comms.bus.reads.mark_displayed(viewer.name, displayed)
    read = wire(tmp_path).views.viewer_snapshot(str(tmp_path))
    assert read.channel_unread["#team"].count == 0
    # Metadata updates preserve identity; removal and a new declaration do not.
    comms.registry.remove(viewer.name)
    comms.registry.register(replace(viewer, created_at=viewer.created_at + 1))
    returned = wire(tmp_path).views.viewer_snapshot(str(tmp_path))
    assert returned.channel_unread["#team"].count == 1
    assert returned.channels == read.channels


def test_checked_display_retains_original_cut_and_persists_changed_scope(tmp_path):
    from test_private_bus_checkpoint import _root
    from test_turn_context_wire import manifest

    comms, _ = _root(tmp_path)
    first = publish_initial(comms, 'sender', '#team', '@Alice first')
    comms.contexts.record_after(UNREAD_CONTEXT, manifest(comms.registry.require('Alice')))
    with comms.views.presentation.snapshot(viewer=NamedViewer('Alice')) as (_, source):
        assert isinstance(source, OpenedWireSnapshot)
        # Original lock has ended. A genuine later publication cannot broaden
        # this descriptor's cut or make the captured observation visible.
        second = publish_initial(comms, 'sender', '#team', '@Alice next')
        assert [message.message_id for message, _ in source.public_records()] == [first.message_id]
    assert source.stream.closed
    assert [message.message_id for message in comms.views.channel_display_page('#any').messages] == [
        first.message_id, second.message_id
    ]

    comms.views.viewer_snapshot(str(tmp_path))
    comms.channels.set_channel_any_mode('#team', True)
    comms.views.viewer_snapshot(str(tmp_path))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    checkpoint = BusDisplayIndex(comms.bus.log.path, viewer).path
    current = json.loads(checkpoint.read_text())
    from agent_comms.field_codec import FieldCodec
    from agent_comms.read_basis import MemberActivityDisplayScope
    with comms.views.presentation.snapshot(viewer=NamedViewer(viewer)) as (basis, _):
        expected_scope = basis.scope('#team')
    assert isinstance(expected_scope, MemberActivityDisplayScope)
    assert next(scope for scope in current['scopes']
                if scope['channel'] == '#team') == FieldCodec.encode(expected_scope)


@pytest.mark.parametrize('kind', ['recorded_context_manifest', 'unknown'])
def test_opened_cut_rejects_context_and_unknown_observations(tmp_path, kind, *, thread_registration):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.context_store import AbsentContext
    from agent_comms.wire_log import WireLog
    from test_turn_context_wire import manifest

    tmp_path.chmod(0o700)
    log = WireLog(tmp_path / 'bus.jsonl')
    from agent_comms.wire_metadata import WireMetadata
    log.write_metadata_unlocked(WireMetadata.private_root('0' * 32))
    owner = thread_registration.new_thread('author', frozenset(), str(tmp_path), created_at=17003.0)
    recorded, _entries = AbsentContext().capture(manifest(owner))
    row = {'observation': {'kind': kind, 'manifest': FieldCodec.encode(recorded)}}
    log.path.write_text(json.dumps(row) + '\n')
    log.path.chmod(0o600)
    with pytest.raises(StoredHistoryUnreadable):
        with log.opened_wire_snapshot(opening=CurrentSnapshotOpening()):
            pytest.fail('Unknown observations must be rejected before exposing a history cut')


def test_reserved_sequence_preserves_committed_read_cut_without_claim(tmp_path):
    from test_private_bus_checkpoint import _root

    comms, _ = _root(tmp_path)
    first = publish_initial(comms, 'sender', '#team', '@Alice first')
    drain_candidate_catchup(comms.root)
    # This is the original durable state between reservation and append.
    # No second input or fabricated successful append resolves that uncertainty.
    with comms.bus.log.locked():
        marker = comms.bus.log.private_marker_unlocked()
        marker.last_seq += 1
        comms.bus.log.write_metadata_unlocked(marker)
    with comms.bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as source:
        assert isinstance(source, OpenedWireSnapshot)
        assert [message.message_id for message, _ in source.public_records()] == [first.message_id]
    with comms.bus.snapshot(viewer=NamedViewer('Alice')) as (basis, source):
        assert source.through == first.seq < source.metadata.last_seq
        assert comms.bus.pending_counts_opened(source, basis) == {'#team': 1}
    with pytest.raises(RelationViolationError):
        with comms.bus.log.checked_read() as source:
            source.require_current()
