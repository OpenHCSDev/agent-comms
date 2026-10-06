"""Canonical page identity covers byte and annotation/source changes."""

import json
import sqlite3
from contextlib import closing

import pytest

from agent_comms.comms import wire
from agent_comms.coordination_errors import StaleRevision
from agent_comms.threads import Thread


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_saved_input_annotation_revokes_page_without_journal_change(tmp_path, journal_mode):
    comms = wire(tmp_path / "wire")
    path = tmp_path / "native.jsonl"
    native_id = "a" * 32
    path.write_text(json.dumps({
        "type": "message", "id": "entry", "message": {
            "role": "user", "inputId": native_id, "content": "private wrapper",
        },
    }) + "\n")
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path), session_file=str(path)))
    comms.transcripts.routes.record_input_display("b" * 32, None)
    with closing(sqlite3.connect(comms.transcripts.routes.database_path)) as connection:
        assert connection.execute("PRAGMA journal_mode=" + journal_mode).fetchone()[0] == journal_mode.lower()
        captured = comms.transcripts.capture_page_read("worker")
        assert captured.read().events[0].declared_name == "user"
        original = path.read_bytes()
        comms.transcripts.routes.record_input_display(native_id, None)
        assert path.read_bytes() == original
        assert not captured.current()
        with pytest.raises(StaleRevision):
            captured.read()
        assert comms.transcripts.capture_page_read("worker").read().events[0].declared_name == "context"


def test_native_append_and_fork_source_change_revoke_prior_read(tmp_path):
    comms = wire(tmp_path / "wire")
    parent = tmp_path / "parent.jsonl"
    parent.write_text(json.dumps({"type": "message", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "parent answer"}],
    }}) + "\n")
    comms.registry.declare(Thread("parent", frozenset(), str(tmp_path), session_file=str(parent)))
    comms.registry.declare(Thread("child", frozenset(), str(tmp_path), parent="parent", task="continue"))
    captured = comms.transcripts.capture_page_read("child")
    assert any(event.text == "parent answer" for event in captured.read().events)
    with parent.open("a") as output:
        output.write(json.dumps({"type": "message", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "later answer"}],
        }}) + "\n")
    with pytest.raises(StaleRevision):
        captured.read()
    fresh = comms.transcripts.capture_page_read("child")
    assert any(event.text == "later answer" for event in fresh.read().events)
    child = tmp_path / "child.jsonl"
    child.write_text(json.dumps({"type": "message", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "child answer"}],
    }}) + "\n")
    comms.threads.attach_session(comms.registry.require("child"), str(child))
    with pytest.raises(StaleRevision):
        fresh.read()
    assert [event.text for event in comms.transcripts.capture_page_read("child").read().events] == ["child answer"]


def test_thread_activity_and_annotations_do_not_replay_saved_content(tmp_path):
    from dataclasses import replace
    from agent_comms.acp_extension import TranscriptSnapshotUpdate

    comms = wire(tmp_path / "wire")
    path = tmp_path / "native.jsonl"
    path.write_text(json.dumps({"type": "message", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "Original retained answer"}],
    }}) + "\n")
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path), session_file=str(path)))
    captured = comms.transcripts.capture_page_read("worker")
    original = captured.read()
    comms.registry.heartbeat("worker")
    assert captured.current()
    comms.registry.register(replace(comms.registry.require("worker"), title="Updated annotation"))
    # The original source owner still fences native bytes, routes, publication
    # proof and incarnation. A heartbeat changes annotations, not those bytes.
    assert not captured.current()
    assert captured.read() == original
    fresh = comms.transcripts.capture_page_read("worker")
    assert fresh.identity.same_content(captured.identity)
    assert TranscriptSnapshotUpdate.capture(comms.transcripts, "worker").page == original
