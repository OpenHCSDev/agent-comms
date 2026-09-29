"""Canonical saved clocks survive projection, ACP transport and page rebuild."""

import json
from dataclasses import replace
from datetime import datetime

import pytest

from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates, encode_updates
from agent_comms.comms import wire
from agent_comms.native_entries import NativeEntry, TranscriptProjection
from agent_comms.routing import TurnRouting
from agent_comms.threads import Thread
from agent_comms.transcript_events import AssistantTranscript, UserTranscript

pytest_plugins = ("test_backend_native_lifecycle",)


@pytest.mark.parametrize("stamp", ["2026-09-28T23:12:33.123Z", "2026-09-28T19:12:33.123-04:00"])
def test_original_record_clock_survives_split_and_routed_projection(tmp_path, stamp):
    comms = wire(tmp_path / "wire")
    for name in ("peer", "owner"):
        comms.threads.register(Thread(name, frozenset(), str(tmp_path)))
    requests = tuple(comms.messaging.send_message("peer", "owner", text) for text in ("first", "second"))
    entry = NativeEntry.from_wire({
        "type": "message", "timestamp": stamp,
        "message": {"role": "user", "content": [
            {"type": "text", "text": "native prompt"},
            {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
        ]},
    })
    expected = datetime.fromisoformat(stamp).timestamp()
    for context in (TranscriptProjection(), TranscriptProjection(routing=TurnRouting(requests))):
        events = entry.events(context)
        assert len(events) == 2
        assert all(event.timestamp == expected for event in events)
        assert entry.timestamp == stamp  # Native external field stays exact.
    compacted = NativeEntry.from_wire({"type": "compaction", "timestamp": stamp, "summary": "kept context"})
    assert compacted.events(TranscriptProjection())[0].timestamp == expected
    first = AssistantTranscript("start", timestamp=expected)
    assert first.merge(replace(first, text="continued")).timestamp == expected
    assert first.merge(replace(first, timestamp=expected + 1)) is None


@pytest.mark.parametrize("stamp", [None, "not-a-time", "2026-09-28T23:12:33"])
def test_unrecorded_clock_remains_unknown_without_losing_history(stamp):
    entry = NativeEntry.from_wire({"type": "message", "timestamp": stamp,
                                   "message": {"role": "user", "content": "preserved"}})
    assert entry.events(TranscriptProjection()) == [UserTranscript("preserved", timestamp=None)]


async def test_actual_native_saved_clocks_survive_acp_snapshot_and_rebuild(native_backend):
    native = native_backend
    assert (await native.run("Saved clock must survive returning to history"))[-1].ok
    original = native.session.read_bytes()
    proof = native.session.with_suffix(native.session.suffix + ".input-proof")
    original_proof = proof.read_bytes()
    records = [json.loads(line) for line in original.splitlines()]
    expected = [datetime.fromisoformat(row["timestamp"]).timestamp()
                for row in records if row["type"] == "message"]
    assert len(expected) == 2
    comms = wire(native.root)
    comms.threads.register(Thread("clock", frozenset(), str(native.project), session_file=str(native.session)))
    page = comms.transcripts.thread_transcript_page("clock")
    assert [event.timestamp for event in page.events] == expected
    metadata = json.loads(json.dumps(encode_updates(TranscriptSnapshotUpdate(page))))
    received = decode_updates(metadata)[0].page
    assert received == page
    rebuilt = wire(native.root).transcripts.thread_transcript_page("clock", through=page.after)
    assert rebuilt == received
    assert native.session.read_bytes() == original
    assert proof.read_bytes() == original_proof
    assert native.provider.posts == 1
    print(f"actual native clocks={expected}; ACP snapshot + rebuild exact; native history/proof unchanged")
