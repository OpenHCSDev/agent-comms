"""Actual saved buses and transcripts, with colliding live/source sequences."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import HistoricalMessage, HistoryCursor
from agent_comms.comms import Comms
from agent_comms.read_ledger import ReadLedger
from agent_comms.routing import MessageRoute, TurnRouting
from agent_comms.threads import Thread


def setup(root, count=5):
    comms = Comms(root)
    comms.threads.register(Thread("alice", frozenset({"team"}), str(root), created_at=10.0))
    comms.threads.register(Thread("bob", frozenset({"team"}), str(root), created_at=11.0))
    comms.messaging.initialize_private_initial_protocol()
    viewer = comms.messaging.user_identity(str(root)).name
    for i in range(count):
        comms.messaging.send("alice", "#team", f"{root.name} channel {i}")
        comms.messaging.send("alice", viewer, f"{root.name} direct {i}")
    return comms


def collect(comms, method, *args, **kwargs):
    page = getattr(comms.views, method)(*args, limit=3, **kwargs)
    result = list(page.messages)
    for _ in range(40):
        if not page.has_older:
            return result
        page = getattr(comms.views, method)(*args, before=page.oldest_cursor, limit=3, **kwargs)
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
    "method,args",
    [
        ("channel_display_page", ("#team",)),
        ("dm_display_page", ("alice",)),
        ("channel_display_page", ("#any",)),
    ],
)
def test_all_history_backward_and_forward(migrated, method, args):
    old, prior, live, source = migrated
    rows = collect(live, method, *args, worktree=str(live.root))
    expected = 18 if args == ("#any",) else 9
    assert len(rows) == expected
    assert len({row.view_key for row in rows}) == expected
    assert len({row.seq for row in rows}) < expected
    assert rows == sorted(rows, key=lambda row: row.view_order)
    forward = []
    cursor = HistoryCursor(source.key, 0)
    for _ in range(40):
        page = getattr(live.views, method)(*args, after=cursor, limit=2, worktree=str(live.root))
        forward.extend(page.messages)
        if not page.has_newer:
            break
        cursor = page.newest_cursor
    assert [m.view_key for m in forward] == [m.view_key for m in rows]


def test_original_identity_and_no_delivery_authority(migrated):
    old, prior, live, source = migrated
    rows = collect(live, "channel_display_page", "#team", worktree=str(live.root))
    historical = [m for m in rows if isinstance(m, HistoricalMessage) and m.source == source]
    original = old.views.channel_history("#team")
    assert [m.to_wire() for m in historical] == [m.to_wire() for m in original]
    assert all(m.sender_created_at == 10.0 for m in historical)
    assert len(live.bus.incoming_page("bob", after=0).messages) == 2
    assert live.bus.log.latest_sequence() == 4
    assert live.views.attach_history(old.root) == source
    assert len(live.bus.history_sources()) == 2


def test_sparse_historical_ack_does_not_ack_live(migrated):
    _, _, live, source = migrated
    viewer = live.messaging.user_identity(str(live.root))
    page = live.views.channel_display_page(
        "#team", before=HistoryCursor(source.key, 100), worktree=str(live.root), limit=3
    )
    live.views.mark_historical_view_read(page.historical_display.select([page.messages[1].seq]))
    assert list(ReadLedger(Path(source.root) / ReadLedger.filename).read().messages.values()) == [
        (page.messages[1].seq,)
    ]
    assert not live.bus.reads.seen_sequences(viewer.name, live.registry.snapshot())
    assert live.bus.pending_count("bob", "#team") == 2
    with pytest.raises(ValueError):
        live.views.mark_historical_view_read(
            replace(
                page.historical_display,
                displayed=replace(page.historical_display.displayed, viewer_created_at=-9.0),
            )
        )


def test_duplicate_and_newer_incarnations_sessions(tmp_path):
    old = setup(tmp_path / "old", 0)
    # Historical invalid-for-live alias timestamps must survive as evidence.
    raw = json.loads((old.root / "registry.json").read_text())
    raw["threads"]["bob"]["created_at"] = 10.0
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
    raw["threads"]["alice"]["session_file"] = str(session)
    (old.root / "registry.json").write_text(json.dumps(raw))
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
        "#team", before=HistoryCursor(source.key, 100), max_bytes=1
    )
    assert len(page.messages) == 1
    assert page.has_older
    with pytest.raises(ValueError):
        live.views.channel_display_page("#team", before=HistoryCursor("detached", 2))


def test_normal_comms_api_contains_history_and_execution_stays_live(migrated):
    _, _, live, _ = migrated
    viewer = live.messaging.user_identity(str(live.root)).name
    assert len(live.views.channel_history("#team")) == 9
    assert len(live.views.dm_history("alice", viewer)) == 9
    assert len(live.views.full_history()) == 18
    for message in live.views.full_history():
        if isinstance(message, HistoricalMessage):
            assert not message.starts_turn
            assert not message.starts_turn_for("bob")
            assert message.display_metadata["history"]["source"] == message.source.original_root
    assert len(live.bus.log.full_history()) == 4


def test_attach_only_reads_original_roots_and_rejects_changed_snapshot(tmp_path):
    old = setup(tmp_path / "old")
    (old.root / "unknown-input.json").write_text('{"verdict":"UNKNOWN"}')
    original = {p.name: p.read_bytes() for p in old.root.iterdir() if p.is_file()}
    live = setup(tmp_path / "live", 1)
    source = live.views.attach_history(old.root)
    assert {p.name: p.read_bytes() for p in old.root.iterdir() if p.is_file()} == original
    assert not (Path(source.root) / "unknown-input.json").exists()
    page = live.views.channel_display_page(
        "#team", before=HistoryCursor(source.key, 100), worktree=str(live.root)
    )
    path = Path(source.root) / "bus.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="snapshot changed"):
        live.views.mark_historical_view_read(page.historical_display)
    with pytest.raises(ValueError, match="snapshot changed"):
        live.views.channel_display_page("#team", before=HistoryCursor(source.key, 100))


def test_older_than_registry_incarnation_stays_unattributed(migrated):
    _, _, live, source = migrated
    original = live.bus.log.full_history()[0]
    projected = HistoricalMessage.project(
        replace(original, timestamp=1.0), source, 0, source.registry().snapshot()
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
