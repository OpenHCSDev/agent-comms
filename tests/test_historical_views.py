"""Actual saved buses and transcripts, with colliding live/source sequences."""

from bus_history import read_history
from agent_comms.channel_targets import CommandTarget
from agent_comms.read_basis import NamedViewer
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.historical_views import HistoricalMessage
from agent_comms.page_traversal import HistoryCursor
from agent_comms.comms import Comms
from agent_comms.coordination_errors import HistoricalSourceChanged
from agent_comms.historical_views import HistoryArchive
from agent_comms.read_basis import ChannelConversation, ConversationCursor, ReadCursor, HistoricalSelection, ChannelSelection, DMSelection
from agent_comms.read_ledger import ReadLedger
from agent_comms.routing import MessageRoute, TurnRouting
from agent_comms.threads import Thread
from agent_comms.message_page import PageBounds
from agent_comms.page_traversal import OlderTraversal


@pytest.mark.parametrize('exists', [False, True])
def test_catalogue_creation_or_change_during_read_is_rejected(tmp_path, exists):
    archive = HistoryArchive(tmp_path / 'bus.jsonl')
    if exists:
        archive.path.write_text('[]')
    with pytest.raises(HistoricalSourceChanged, match='catalogue changed'):
        with archive.read_sources() as sources:
            assert sources == ()
            archive.path.write_text('[ ]')


def test_missing_catalogue_is_empty_without_creating_it(tmp_path):
    archive = HistoryArchive(tmp_path / 'bus.jsonl')
    assert archive.sources() == ()
    assert not archive.path.exists()


def setup(root, count=5):
    comms = Comms(root)
    comms.registry.declare(comms.registry.new_thread('alice', frozenset({'team'}), str(root), created_at=10.0))
    comms.registry.declare(comms.registry.new_thread('bob', frozenset({'team'}), str(root), created_at=11.0))
    comms.messaging.initialize_private_initial_protocol()
    viewer = comms.messaging.user_identity(str(root)).name
    for i in range(count):
        comms.messaging.send("alice", "#team", f"{root.name} channel {i}").message.message_id
        comms.messaging.send("alice", viewer, f"{root.name} direct {i}").message.message_id
    return comms


def collect(comms, method, *args, **kwargs):
    page = getattr(comms.views, method)(*args, bounds=PageBounds(limit=3), **kwargs)
    result = list(page.messages)
    for _ in range(40):
        if not page.has_older:
            return result
        page = getattr(comms.views, method)(*args, bounds=PageBounds(page.oldest_cursor.older(), 3), **kwargs)
        result[0:0] = page.messages
    raise AssertionError("paging did not terminate")


@pytest.fixture
def migrated(tmp_path):
    old = setup(tmp_path / "old")
    prior = setup(tmp_path / "prior", 2)
    live = setup(tmp_path / "live", 2)
    before = (live.root / "bus.jsonl").read_bytes()
    source = live.views.attach_history(old.root)
    live.views.attach_history(prior.root)
    assert (live.root / "bus.jsonl").read_bytes() == before
    return old, prior, live, source


@pytest.mark.parametrize(
    "method,args,reader",
    [
        ("channel_display_page", ("#team",), lambda live: dict(viewer=live.views.display_viewer(str(live.root)))),
        ("dm_display_page", ("alice",), lambda live: dict(worktree=str(live.root))),
        ("channel_display_page", ("#any",), lambda live: dict(viewer=live.views.display_viewer(str(live.root)))),
    ],
)
def test_all_history_backward_and_forward(migrated, method, args, reader):
    old, prior, live, source = migrated
    scope = reader(live)
    rows = collect(live, method, *args, **scope)
    expected = 18 if args == ("#any",) else 9
    assert len(rows) == expected
    assert len({row.view_key for row in rows}) == expected
    assert len({row.seq for row in rows}) < expected
    assert rows == sorted(rows, key=lambda row: row.view_order)
    forward = []
    cursor = HistoryCursor(source.key, 0)
    for _ in range(40):
        page = getattr(live.views, method)(*args, bounds=PageBounds(cursor.newer(), 2), **scope)
        forward.extend(page.messages)
        if not page.has_newer:
            break
        cursor = page.newest_cursor
    assert [m.view_key for m in forward] == [m.view_key for m in rows]


def test_original_identity_and_no_delivery_authority(migrated):
    old, prior, live, source = migrated
    rows = collect(live, "channel_display_page", "#team", viewer=live.views.display_viewer(str(live.root)))
    historical = [m for m in rows if isinstance(m, HistoricalMessage) and m.source == source]
    original = read_history(old, "#team")
    assert [m.to_wire() for m in historical] == [m.to_wire() for m in original]
    assert all(m.sender_created_at == 10.0 for m in historical)
    assert len(live.bus.incoming_page("bob", after=0).messages) == 2
    assert live.bus.log.latest_sequence() == 4
    assert live.views.attach_history(old.root) == source
    assert len(live.bus.history.sources()) == 2


def test_sparse_historical_ack_does_not_ack_live(migrated):
    _, _, live, source = migrated
    viewer = live.messaging.user_identity(str(live.root))
    page = live.views.channel_display_page(
        "#team", viewer=NamedViewer(live.messaging.user_identity(str(live.root)).name),
        bounds=PageBounds(OlderTraversal(100, source.key), 3),
    )
    live.views.mark_page_read(page.select([page.messages[1].seq]), worktree=str(live.root))
    snapshot = live.registry.snapshot()
    assert ReadLedger.at(source.root).read().messages == {
        viewer.incarnation: ReadCursor({
            ChannelConversation('#team'): ConversationCursor(exceptions=(page.messages[1].seq,)),
        })
    }
    assert live.bus.reads.cursor(viewer.name, snapshot) == ReadCursor()
    assert live.bus.pending_count("bob", CommandTarget.named("#team")) == 2
    with pytest.raises(ValueError):
        live.views.mark_page_read(
            replace(page, selection=replace(page.selection, displayed=replace(
                page.selection.displayed,
                displayed=replace(page.selection.displayed.displayed, viewer_created_at=-9.0),
            ))), worktree=str(live.root),
        )


@pytest.mark.parametrize("conversation", ["channel", "dm"])
@pytest.mark.parametrize("archived", [False, True])
def test_select_painted_page_keeps_its_source_with_attached_history(migrated, conversation, archived):
    _, _, live, source = migrated
    bounds = PageBounds(OlderTraversal(100, source.key), 2) if archived else PageBounds(limit=2)
    if conversation == "channel":
        page = live.views.channel_display_page(
            "#team", viewer=live.views.display_viewer(str(live.root)), bounds=bounds,
        )
    else:
        page = live.views.dm_display_page("alice", worktree=str(live.root), bounds=bounds)
    message = page.messages[-1]
    selected = page.select((message.seq,))
    assert selected.messages == (message,)
    assert selected.selection.history_revision == page.selection.history_revision
    if archived:
        assert isinstance(selected.selection, HistoricalSelection)
        assert selected.selection.displayed.source == source
    else:
        assert isinstance(selected.selection, (ChannelSelection, DMSelection))


def test_duplicate_and_newer_incarnations_sessions(tmp_path):
    old = setup(tmp_path / "old", 0)
    # Historical invalid-for-live alias timestamps must survive as evidence.
    # Author the recorded ambiguous namespace through the original typed store;
    # a raw registry overwrite would invalidate its private integrity guard.
    session = tmp_path / "saved.jsonl"
    records = [
        {"type": "session", "version": 3, "id": "saved", "timestamp": "2026-09-20T00:00:00Z"},
        {
            "type": "message",
            "id": "one",
            "message": {
                "role": "user",
                "inputId": "a" * 32,
                "content": [{"type": "text", "text": "old question"}],
            },
        },
        {
            "type": "message",
            "id": "two",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
        },
    ]
    session.write_text("".join(json.dumps(r) + "\n" for r in records))
    with old.registry.store.editing() as edit:
        edit.document.threads["bob"] = replace(edit.document.threads["bob"], created_at=10.0)
        edit.document.threads["alice"] = replace(edit.document.threads["alice"], session_file=str(session))
        edit.commit()
    routing = TurnRouting(reply=MessageRoute("alice", ("#team",)))
    old.transcripts.routes.record(str(session), ("two",), routing)
    old.transcripts.routes.record_input_display("a" * 32, "original owner question")
    live = setup(tmp_path / "live", 0)
    newer = replace(live.registry.require("alice"), created_at=20.0)
    live.registry.unregister("alice")
    live.registry.remove("alice")
    live.registry.register(newer)
    source = live.views.attach_history(old.root)
    # Later source annotations cannot change the already attached snapshot.
    old.transcripts.routes.record(
        str(session), ("two",), TurnRouting(reply=MessageRoute("alice", ("#later",)))
    )
    declarations = {h.thread.name: h.thread for h in live.views.historical_threads()}
    assert declarations["alice"].created_at == declarations["bob"].created_at == 10.0
    assert live.registry.require("alice").created_at == 20.0
    page = live.transcripts.thread_transcript_page(
        "alice", historical_source=source.key, max_messages=1
    )
    assert [e.text for e in page.events] == ["old answer"]
    assert page.events[0].routing == routing
    earlier = live.transcripts.thread_transcript_page(
        "alice", historical_source=source.key, before=page.before, max_messages=1
    )
    assert [e.text for e in earlier.events if e.declared_name == "user"] == [
        "original owner question"
    ]
    assert session.read_text() == "".join(json.dumps(r) + "\n" for r in records)


def test_byte_boundaries_and_stale_cursor(migrated):
    _, _, live, source = migrated
    page = live.views.channel_display_page(
        "#team", bounds=PageBounds(OlderTraversal(100, source.key), max_bytes=1)
    )
    assert len(page.messages) == 1
    assert page.has_older
    with pytest.raises(HistoricalSourceChanged):
        live.views.channel_display_page("#team", bounds=PageBounds(OlderTraversal(2, "detached")))


def test_recorded_transcript_witness_roundtrip_keeps_original_namespace(tmp_path):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.transcripts import TranscriptReadIdentity, RecordedTranscriptReadIdentity

    old, live = setup(tmp_path / "old", 1), setup(tmp_path / "live", 0)
    native = tmp_path / "recorded.jsonl"
    native.write_text(json.dumps({"type": "message", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "original recorded answer"}],
    }}) + "\n")
    old.threads.attach_session((attachment_owner := old.registry.require("alice")), str(native), process=attachment_owner.process_identity)
    old.registry.rename("alice", "recorded-alice")
    replacement = replace(live.registry.require("alice"), created_at=20.0)
    live.registry.unregister("alice")
    live.registry.remove("alice")
    live.registry.register(replacement)
    source = live.views.attach_history(old.root)
    original_registry = (old.root / "registry.json").read_bytes()
    original_native = native.read_bytes()

    read = live.transcripts.capture_page_read("alice", historical_source=source.key)
    assert isinstance(read.identity, RecordedTranscriptReadIdentity)
    assert read.identity.thread.incarnation == old.registry.require("recorded-alice").incarnation
    decoded = FieldCodec.decode(TranscriptReadIdentity, FieldCodec.encode(read.identity))
    assert decoded == read.identity
    assert set(TranscriptReadIdentity.names()) == {"live", "recorded"}
    assert FieldCodec.value_schema(type[TranscriptReadIdentity])["enum"] == list(TranscriptReadIdentity.names())
    with pytest.raises(ValueError, match="Unknown"):
        FieldCodec.decode(TranscriptReadIdentity, {**FieldCodec.encode(read.identity), "kind": "unregistered"})
    rebound = live.transcripts.bind_page_read("recorded-alice", decoded)
    assert rebound.current_identity() == decoded
    assert any(event.text == "original recorded answer" for event in rebound.read().events)
    delivered = []
    decoded.publish_turn(lambda root, thread: delivered.append((root, thread)))
    assert delivered == []
    live_identity = live.transcripts.capture_page_read("alice").identity
    live_identity.publish_turn(lambda root, thread: delivered.append((root, thread)))
    assert delivered == [(str(live.root), live.registry.require("alice"))]
    assert (old.root / "registry.json").read_bytes() == original_registry
    assert native.read_bytes() == original_native




def test_detached_recorded_witness_cannot_rebind_to_same_name_live_thread(migrated):
    from agent_comms.coordination_errors import StaleRevision

    _old, _prior, live, source = migrated
    read = live.transcripts.capture_page_read("alice", historical_source=source.key)
    with pytest.raises(StaleRevision, match="original thread"):
        live.transcripts.bind_page_read("bob", read.identity)
    live.bus.history.path.write_text("[]")
    with pytest.raises(HistoricalSourceChanged, match="detached"):
        read.current_identity()
    with pytest.raises(HistoricalSourceChanged, match="detached"):
        live.transcripts.bind_page_read("alice", read.identity)


def test_normal_comms_api_contains_history_and_execution_stays_live(migrated):
    _, _, live, _ = migrated
    viewer = live.messaging.user_identity(str(live.root)).name
    assert len(collect(live, "channel_history_page", "#team")) == 9
    assert len(collect(live, "history_page", CommandTarget.named("alice"), viewer=viewer)) == 9
    assert len(collect(live, "full_history_page")) == 18
    for message in collect(live, "full_history_page"):
        if isinstance(message, HistoricalMessage):
            assert not message.role.response_policy(message).starts_turn
            assert not message.role.response_policy(message).starts_turn_for(message, "bob")
            assert message.display_metadata["history"]["source"] == message.source.original_root
    assert len(read_history(live)) == 4


def test_attach_only_reads_original_roots_and_rejects_changed_snapshot(tmp_path):
    old = setup(tmp_path / "old")
    (old.root / "unknown-input.json").write_text('{"verdict":"UNKNOWN"}')
    original = {p.name: p.read_bytes() for p in old.root.iterdir() if p.is_file()}
    live = setup(tmp_path / "live", 1)
    source = live.views.attach_history(old.root)
    assert {p.name: p.read_bytes() for p in old.root.iterdir() if p.is_file()} == original
    assert not (Path(source.root) / "unknown-input.json").exists()
    page = live.views.channel_display_page(
        "#team", viewer=NamedViewer(live.messaging.user_identity(str(live.root)).name),
        bounds=PageBounds(OlderTraversal(100, source.key)),
    )
    path = Path(source.root) / "bus.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="snapshot changed"):
        live.views.mark_page_read(page, worktree=str(live.root))
    with pytest.raises(ValueError, match="snapshot changed"):
        live.views.channel_display_page(
            "#team", bounds=PageBounds(OlderTraversal(100, source.key))
        )


def test_older_than_registry_incarnation_stays_unattributed(migrated):
    _, _, live, source = migrated
    original = read_history(live)[0]
    projected = HistoricalMessage.project(
        replace(original, timestamp=1.0), source, 0, source.provenance
    )
    assert projected.sender_created_at is None
    assert projected.timestamp == 1.0


def test_missing_historical_peer_is_browsable_without_live_registration(migrated):
    _, _, live, source = migrated
    live.registry.unregister("alice")
    live.registry.remove("alice")
    page = live.views.dm_display_page("alice", worktree=str(live.root))
    assert page.messages
    assert all(isinstance(message, HistoricalMessage) for message in page.messages)
    assert "alice" not in live.registry


def test_historical_namespace_has_recorded_provenance_and_no_live_authority(migrated):
    from dataclasses import fields
    from agent_comms.field_codec import FieldCodec
    from agent_comms.registry_provenance import RegistryProvenance
    from agent_comms.thread_identity import ThreadProvenance

    old, _, live, source = migrated
    recorded = source.provenance.require("alice")
    assert type(recorded) is ThreadProvenance
    assert recorded.incarnation == old.registry.require("alice").incarnation
    assert set(FieldCodec.encode(recorded)) == {
        item.name for item in fields(ThreadProvenance) if item.init and item.name != 'backend'
    }
    assert not hasattr(recorded, "process_identity")
    assert not hasattr(recorded, "active_turn")
    assert not hasattr(source.provenance, "claimed_generations")
    # Required creation is parsed before any constructor clock/default can run.
    raw = FieldCodec.encode(recorded)
    raw.pop("created_at")
    with pytest.raises(ValueError, match="Missing required"):
        FieldCodec.decode(ThreadProvenance, raw)
    # Original finite historical timestamps are preserved, never synthesized.
    zero = replace(recorded, created_at=0.0)
    archived_zero = RegistryProvenance(threads={zero.name: zero}, aliases={})
    assert FieldCodec.decode(RegistryProvenance, FieldCodec.encode(archived_zero)) == archived_zero
    aliased = RegistryProvenance(threads={"alice": recorded}, aliases={"original-alice": "alice"})
    assert aliased.require("original-alice") is recorded
    with pytest.raises(ValueError):
        FieldCodec.decode(ThreadProvenance, {**FieldCodec.encode(recorded), "active_turn": None})
    # Source identity is separate from today's live owner, including renames.
    before = FieldCodec.encode(source.provenance)
    live.registry.rename("alice", "new-alice")
    assert FieldCodec.encode(source.provenance) == before
    assert live.views.historical_threads("alice")


@pytest.mark.parametrize('changed_resource', ['bus', 'certificate', 'pending'])
def test_archived_certificate_remains_immutable_without_writer_recovery(migrated, changed_resource):
    """Archive reads reject mutation/pending ownership instead of adopting it."""
    import sqlite3
    from agent_comms.checkpoint_seals import PendingSeal
    from agent_comms.errors import RelationViolationError
    from agent_comms.wire_log import WireLog

    _, _, _, source = migrated
    log = WireLog(Path(source.root) / 'bus.jsonl')
    with log.certified_read() as opened:
        saved = opened.witness
        assert opened.connection.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            opened.connection.execute('DELETE FROM prefix_certificate')
    if changed_resource == 'bus':
        log.path.write_bytes(log.path.read_bytes() + b'\n')
    elif changed_resource == 'certificate':
        with sqlite3.connect(log.path.with_name('private_bus_checkpoint.sqlite3')) as db:
            db.execute('CREATE TABLE unrelated(value TEXT)')
    else:
        marker = log.read_metadata_unlocked(required=True)
        marker.seal_with(PendingSeal.capture(saved, saved,
                                            log.path.with_name('private_bus_checkpoint.sqlite3')))
        log.write_metadata_unlocked(marker)
    before = {p: p.read_bytes() for p in (log.path, log.metadata_path,
                                         log.path.with_name('private_bus_checkpoint.sqlite3'))}
    with pytest.raises(RelationViolationError):
        with log.certified_read():
            pass
    assert {p: p.read_bytes() for p in before} == before
