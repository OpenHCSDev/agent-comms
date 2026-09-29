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
    comms.threads.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(path)))
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
        "role": "assistant", "content": "parent answer",
    }}) + "\n")
    comms.threads.register(Thread("parent", frozenset(), str(tmp_path), session_file=str(parent)))
    comms.threads.register(Thread("child", frozenset(), str(tmp_path), parent="parent", task="continue"))
    captured = comms.transcripts.capture_page_read("child")
    assert any(event.text == "parent answer" for event in captured.read().events)
    with parent.open("a") as output:
        output.write(json.dumps({"type": "message", "message": {
            "role": "assistant", "content": "later answer",
        }}) + "\n")
    with pytest.raises(StaleRevision):
        captured.read()
    fresh = comms.transcripts.capture_page_read("child")
    assert any(event.text == "later answer" for event in fresh.read().events)
    child = tmp_path / "child.jsonl"
    child.write_text(json.dumps({"type": "message", "message": {
        "role": "assistant", "content": "child answer",
    }}) + "\n")
    comms.threads.attach_session("child", str(child))
    with pytest.raises(StaleRevision):
        fresh.read()
    assert [event.text for event in comms.transcripts.capture_page_read("child").read().events] == ["child answer"]
