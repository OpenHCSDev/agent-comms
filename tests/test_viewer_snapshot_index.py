"""A fresh sidebar view reuses validated bus history rather than decoding it."""

import json
import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from agent_comms.bus_display_index import BusDisplayIndex
from agent_comms.errors import RelationViolationError
from agent_comms.comms import wire
from agent_comms.messages import Message, MessageType
from agent_comms.threads import Thread


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
    if not indexed:
        # Genuine disposable-store refusal; canonical source remains readable.
        comms.bus.log.path.with_name("bus_route_counts.sqlite3").mkdir()
    with comms.views.presentation.snapshot(viewer=viewer.name) as (basis, source):
        before = comms.bus.pending_counts_opened(source, basis)
        sent = comms.views.last_sent_timestamps(source=source, registry=basis.registry)
        assert before == {"#team": 1}
        comms.bus.reads.mark_displayed(viewer.name, comms.bus.reads.capture(
            viewer.name, (channel,), comms.registry.snapshot(), comms.bus.log.path))
        comms.messaging.send_message("bob", viewer.name, "later direct")
        comms.registry.rename("bob", "renamed-bob")
        assert comms.bus.pending_counts_opened(source, basis) == before
        assert comms.views.last_sent_timestamps(source=source, registry=basis.registry) == sent
        assert "bob" in sent and "renamed-bob" not in sent
    latest = comms.views.viewer_snapshot(str(tmp_path))
    assert latest.unread == {"renamed-bob": 1}
    assert "renamed-bob" in latest.last_sent and "bob" not in latest.last_sent


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
    assert fresh.views.viewer_snapshot(str(tmp_path)) == wire(tmp_path).views.viewer_snapshot(str(tmp_path))


def test_display_checkpoint_rebuilds_but_bus_replacement_is_refused(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("alice", frozenset({"team"}), str(tmp_path)))
    comms.registry.declare(Thread("bob", frozenset({"team"}), str(tmp_path / "bob")))
    comms.messaging.send("bob", "#team", "first")
    comms.messaging.send("bob", "#team", "second")
    assert comms.views.viewer_snapshot(str(tmp_path)).channel_unread["#team"] == 2

    checkpoint = BusDisplayIndex(comms.bus.log.path, comms.messaging.user_identity(str(tmp_path)).name).path
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


def test_display_read_changes_fall_back_when_page_coverage_is_absent(tmp_path):
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

    with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
        after = wire(tmp_path).views.viewer_snapshot(str(tmp_path))
        assert after.channel_unread["#team"] == 9
        assert decode.call_count == 10
    assert after.channels == before.channels
    assert not index_path.exists()


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
    bus_path = comms.bus.log.path
    appended = []
    original = comms.bus.log._opened_wire_snapshot

    @contextmanager
    def append_before_open(*args, **kwargs):
        # _display_snapshot already holds the wire lock at this boundary.
        appended.append(comms.bus.publisher.publish_ordinary(Message("bob", "#team", "raced append", MessageType.INFO)))
        with original(*args, **kwargs) as boundary:
            yield boundary

    monkeypatch.setattr(comms.bus.log, '_opened_wire_snapshot', append_before_open)
    snapshot = comms.views.viewer_snapshot(str(tmp_path))
    team = next(view for view in snapshot.channels if view.channel.name == "#team")
    assert snapshot.channel_unread["#team"] == 2
    assert team.last_activity == appended[0].timestamp


def test_reopened_human_pending_routes_keep_sparse_reads_aliases_and_fallback(tmp_path):
    import sqlite3
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
    delivery = comms.bus._delivery_scope(viewer)
    expected = Counter(
        delivery.conversation(message.sender, message.target) for message in comms.bus.inbox(viewer)
    )
    # Rename preserves sparse read evidence through the canonical thread alias.
    # The untagged human inbox still excludes channel rows.
    assert expected == {"renamed-bob": 2}
    with patch.object(Message, "from_wire", wraps=Message.from_wire) as decode:
        assert wire(tmp_path).bus.pending_counts(viewer) == expected
        assert decode.call_count == 0
    with patch.object(BusRouteCounts, "sync", side_effect=sqlite3.DatabaseError("unavailable")):
        assert wire(tmp_path).bus.pending_counts(viewer) == expected


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
    assert [message.message_id for message in comms.bus.log.full_history()] == [first.message_id, second.message_id]

    comms.views.viewer_snapshot(str(tmp_path))
    comms.channels.set_channel_any_mode('#team', True)
    comms.views.viewer_snapshot(str(tmp_path))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    checkpoint = BusDisplayIndex(comms.bus.log.path, viewer).path
    current = json.loads(checkpoint.read_text())
    assert next(scope for scope in current['semantics']['scopes']
                if scope['channel'] == '#team')['any_mode'] is True


def test_uncertified_opened_cut_still_rejects_unknown_observation(tmp_path):
    from agent_comms.wire_log import WireLog, OpenedWireSnapshot
    from agent_comms.wire_record import ObservationWireRecord, ContextManifestWireObservation
    from test_turn_context_wire import manifest

    tmp_path.chmod(0o700)
    log = WireLog(tmp_path / 'bus.jsonl')
    from agent_comms.wire_metadata import WireMetadata
    log.write_metadata_unlocked(WireMetadata(writer_protocol_version=1, wire_root_id='0' * 32))
    owner = Thread('author', frozenset(), str(tmp_path), created_at=17003.0)
    row = ObservationWireRecord(ContextManifestWireObservation(manifest(owner)))
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
    with pytest.raises(RelationViolationError, match='differs from its durable marker'):
        with comms.bus.log.certified_read() as source:
            source.require_current()
