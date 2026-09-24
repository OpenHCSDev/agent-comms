"""Transcript routing lookups stay bounded as saved history grows."""

import json
from pathlib import Path

from agent_comms import MessageRoute, Thread, TurnRouting, wire


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
    comms.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    routing = TurnRouting(reply=MessageRoute("worker", ("#team",)))
    comms.transcript_routes.record(
        str(session), tuple(f"entry-{index}" for index in range(10_000)), routing
    )

    decoded = 0
    real_decode = TurnRouting.from_wire

    def counted_decode(payload):
        nonlocal decoded
        decoded += 1
        return real_decode(payload)

    monkeypatch.setattr(TurnRouting, "from_wire", staticmethod(counted_decode))
    page = wire(tmp_path / "wire").thread_transcript_page("worker")
    assert [event.text for event in page.events] == [
        f"answer {index}" for index in range(9980, 10_000)
    ]
    assert all(event.routing == routing for event in page.events)
    assert decoded <= 25


def test_legacy_json_routes_migrate_without_losing_new_records(tmp_path) -> None:
    session = tmp_path / "session.jsonl"
    _session(session, 2)
    comms = wire(tmp_path / "wire")
    comms.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    old = TurnRouting(reply=MessageRoute("worker", ("#old",)))
    new = TurnRouting(reply=MessageRoute("worker", ("#new",)))
    legacy = comms.root / "transcript_routes.json"
    legacy.write_text(json.dumps({str(session): {"entry-0": old.to_wire()}}))

    reopened = wire(comms.root)
    reopened.transcript_routes.record(str(session), ("entry-1",), new)
    page = wire(comms.root).thread_transcript_page("worker")
    assert [event.routing for event in page.events] == [old, new]


def test_running_legacy_writer_is_imported_without_overwriting_new_routes(tmp_path) -> None:
    session = tmp_path / "session.jsonl"
    _session(session, 3)
    comms = wire(tmp_path / "wire")
    comms.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    old = TurnRouting(reply=MessageRoute("worker", ("#old",)))
    modern = TurnRouting(reply=MessageRoute("worker", ("#modern",)))
    late = TurnRouting(reply=MessageRoute("worker", ("#late",)))
    legacy = comms.root / "transcript_routes.json"
    legacy.write_text(json.dumps({str(session): {"entry-0": old.to_wire()}}))
    assert comms.thread_transcript_page("worker").events[0].routing == old
    comms.transcript_routes.record(str(session), ("entry-1",), modern)

    # An older process can still publish its atomic JSON snapshot while this
    # process is running. Its newly annotated entry must enter the index.
    legacy.write_text(
        json.dumps(
            {
                str(session): {
                    "entry-0": late.to_wire(),  # legacy-owned route was updated
                    "entry-1": old.to_wire(),  # stale view of the new writer's entry
                    "entry-2": late.to_wire(),
                }
            }
        )
    )
    page = comms.thread_transcript_page("worker")
    assert [event.routing for event in page.events] == [late, modern, late]
