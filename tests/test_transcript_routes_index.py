"""Transcript routing lookups stay bounded as saved history grows."""

import json
import sqlite3
from pathlib import Path

from agent_comms.comms import wire
from agent_comms.transcript_events import TranscriptCodec
from agent_comms.routing import MessageRoute, TurnRouting
from agent_comms.threads import Thread


def _session(path: Path, count: int) -> None:
    with path.open("w") as output:
        for index in range(count):
            output.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": f"entry-{index}",
                        "message": {"role": "assistant", "content": f"answer {index}"},
                    }
                )
                + "\n"
            )


def test_tail_page_decodes_only_its_routing_entries(tmp_path, monkeypatch) -> None:
    session = tmp_path / "session.jsonl"
    _session(session, 10_000)
    comms = wire(tmp_path / "wire")
    comms.threads.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    routing = TurnRouting(reply=MessageRoute("worker", ("#team",)))
    comms.transcripts.routes.record(
        str(session), tuple(f"entry-{index}" for index in range(10_000)), routing
    )

    decoded = 0
    real_decode = TranscriptCodec.decode

    def counted_decode(target, payload):
        nonlocal decoded
        decoded += int(target is TurnRouting)
        return real_decode(target, payload)

    monkeypatch.setattr(TranscriptCodec, "decode", staticmethod(counted_decode))
    page = wire(tmp_path / "wire").transcripts.thread_transcript_page("worker")
    assert [event.text for event in page.events] == [
        f"answer {index}" for index in range(9980, 10_000)
    ]
    assert all(event.routing == routing for event in page.events)
    assert decoded <= 25


def test_legacy_json_routes_migrate_without_losing_new_records(tmp_path) -> None:
    session = tmp_path / "session.jsonl"
    _session(session, 2)
    comms = wire(tmp_path / "wire")
    comms.threads.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    old = TurnRouting(reply=MessageRoute("worker", ("#old",)))
    new = TurnRouting(reply=MessageRoute("worker", ("#new",)))
    legacy = comms.root / "transcript_routes.json"
    legacy.write_text(json.dumps({str(session): {"entry-0": old.to_wire()}}))

    reopened = wire(comms.root)
    reopened.transcripts.routes.record(str(session), ("entry-1",), new)
    page = wire(comms.root).transcripts.thread_transcript_page("worker")
    assert [event.routing for event in page.events] == [old, new]


def test_one_way_import_preserves_indexed_routes_and_retires_source_column(tmp_path):
    session = tmp_path / "session.jsonl"
    _session(session, 3)
    comms = wire(tmp_path / "wire")
    comms.threads.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    old = TurnRouting(reply=MessageRoute("worker", ("#old",)))
    modern = TurnRouting(reply=MessageRoute("worker", ("#modern",)))
    late = TurnRouting(reply=MessageRoute("worker", ("#late",)))
    saved = comms.root / "transcript_routes.json"
    saved.write_text(
        json.dumps(
            {
                str(session): {
                    "entry-0": late.to_wire(),
                    "entry-1": old.to_wire(),
                    "entry-2": late.to_wire(),
                }
            }
        )
    )
    path = comms.root / "transcript_routes.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE routes (session_file TEXT, entry_id TEXT, route TEXT, source TEXT, PRIMARY KEY(session_file, entry_id))"
        )
        db.executemany(
            "INSERT INTO routes VALUES (?, ?, ?, ?)",
            [
                (str(session), "entry-0", json.dumps(old.to_wire()), "legacy"),
                (str(session), "entry-1", json.dumps(modern.to_wire()), "indexed"),
            ],
        )
    page = comms.transcripts.thread_transcript_page("worker")
    assert [event.routing for event in page.events] == [late, modern, late]
    with sqlite3.connect(path) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(routes)")] == [
            "session_file",
            "entry_id",
            "route",
        ]
    saved.write_text("not read again")
    assert [
        event.routing
        for event in wire(comms.root).transcripts.thread_transcript_page("worker").events
    ] == [late, modern, late]
