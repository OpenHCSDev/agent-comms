"""Saved Pi incoming routes require owner-recorded entry provenance, not prompt similarity."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_comms import Thread, TurnRouting, wire
from agent_comms.declarations import ScheduledTurn


def _entry(message, *, text=None, row_id="input"):
    return {
        "type": "message",
        "id": row_id,
        "timestamp": (datetime.now(UTC) + timedelta(seconds=2)).isoformat(),
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": (
                ScheduledTurn.incoming(message).prompt if text is None else text
            )}],
        },
    }


def _session(path: Path, *entries):
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))


def _case(tmp_path):
    root = tmp_path / "wire"
    session = tmp_path / "session.jsonl"
    comms = wire(root)
    for name in ("peer", "owner", "other"):
        comms.register(Thread(name, frozenset(), str(tmp_path), session_file=(
            str(session) if name == "owner" else None
        )))
    return comms, session


def test_unbound_saved_whole_prompt_stays_plain_until_exact_entry_is_annotated(tmp_path):
    comms, session = _case(tmp_path)
    message = comms.send_message("peer", "owner", "hello")
    exact = _entry(message)
    copied = _entry(message, text=ScheduledTurn.incoming(message).prompt + "\nMore", row_id="copy")
    header = _entry(message, text="[agent-comms from peer to owner]", row_id="header")
    _session(session, copied, header, exact)
    page = comms.thread_transcript_page("owner")
    assert len(page.events) == 3
    assert all(event.routing is None for event in page.events)
    assert page.before.offset == 0
    assert page.after.offset == session.stat().st_size
    comms.transcript_routes.record(str(session), ("input",), TurnRouting((message,), None))
    annotated = comms.thread_transcript_page("owner")
    assert annotated.events[0].routing is None
    assert annotated.events[1].routing is None
    assert annotated.events[2].routing.requests == (message,)
    assert annotated.events[2].routing.reply is None
    # Read-only projection does not mark the DM delivered.
    assert [m.message_id for m in comms.inbox("owner")] == [message.message_id]


def test_attached_foreign_session_with_identical_message_never_infers_local_from(tmp_path):
    source_root = tmp_path / "B"
    source_session = tmp_path / "B-pi-session.jsonl"
    source = wire(source_root)
    for name in ("peer", "owner"):
        source.register(Thread(name, frozenset(), str(tmp_path), session_file=(
            str(source_session) if name == "owner" else None
        )))
    foreign = source.send_message("peer", "owner", "the same routine message")
    _session(source_session, _entry(foreign))

    viewer = wire(tmp_path / "A")
    for name in ("peer", "owner"):
        viewer.register(Thread(name, frozenset(), str(tmp_path)))
    local = viewer.send_message("peer", "owner", "the same routine message")
    assert local.message_id != foreign.message_id
    assert ScheduledTurn.incoming(local).prompt == ScheduledTurn.incoming(foreign).prompt
    viewer.attach_session("owner", str(source_session))
    page = viewer.thread_transcript_page("owner")
    assert len(page.events) == 1
    assert page.events[0].routing is None
    assert [m.message_id for m in viewer.inbox("owner")] == [local.message_id]


def test_foreign_recipient_sender_deletion_and_duplicate_prompt_fail_closed(tmp_path):
    comms, session = _case(tmp_path)
    message = comms.send_message("peer", "other", "not for owner")
    _session(session, _entry(message))
    assert comms.thread_transcript_page("owner").events[0].routing is None
    message = comms.send_message("peer", "owner", "duplicate")
    comms.send_message("peer", "owner", "duplicate")
    _session(session, _entry(message))
    assert comms.thread_transcript_page("owner").events[0].routing is None
    comms.registry.remove("peer")
    assert comms.thread_transcript_page("owner").events[0].routing is None


def test_imported_and_inherited_sessions_never_gain_legacy_route(tmp_path):
    comms, session = _case(tmp_path)
    message = comms.send_message("peer", "owner", "hello")
    _session(session, _entry(message))
    comms.register(Thread("child", frozenset(), str(tmp_path), parent="owner"))
    inherited = comms.thread_transcript_page("child")
    assert inherited.events[0].routing is None
    imported = comms.root / "imported_sessions" / "historical.jsonl"
    imported.parent.mkdir(exist_ok=True)
    _session(imported, _entry(message))
    comms.attach_session("owner", str(imported))
    assert comms.thread_transcript_page("owner").events[0].routing is None


def test_existing_stored_route_wins_and_plain_pages_never_scan_bus(tmp_path, monkeypatch):
    comms, session = _case(tmp_path)
    message = comms.send_message("peer", "owner", "hello")
    _session(session, _entry(message))
    comms.transcript_routes.record(
        str(session), ("input",), __import__("agent_comms").TurnRouting((message,), None)
    )
    monkeypatch.setattr(comms.bus, "_record_snapshot", lambda **_: (_ for _ in ()).throw(
        AssertionError("Transcript pages should not scan the bus")
    ))
    assert comms.thread_transcript_page("owner").events[0].routing.requests == (message,)
    _session(session, {"type": "message", "id": "ordinary", "message": {
        "role": "user", "content": "hello"
    }})
    assert comms.thread_transcript_page("owner").events[0].routing is None
