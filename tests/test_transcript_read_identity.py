"""Canonical page identity covers byte and annotation/source changes."""

import json
import sqlite3
from contextlib import closing

import pytest

from agent_comms.session_file import RegistrySessionLocation
from bus_record_fixture import FIRST_INPUT_ID, SECOND_INPUT_ID
from conftest import installed_comms
from agent_comms.coordination_errors import StaleRevision
from agent_comms.thread_text import ForkedParent, GivenThreadText
from agent_comms.threads import Thread
from agent_comms.transcript_routes import NoDisplayText


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_saved_input_annotation_revokes_page_without_journal_change(tmp_path, journal_mode):
    comms = installed_comms(tmp_path / "wire")
    path = tmp_path / "native.jsonl"
    native_id = FIRST_INPUT_ID
    path.write_text(json.dumps({
        "type": "message", "id": "entry", "timestamp": "2026-10-19T00:00:00Z", "message": {
            "role": "user", "inputId": native_id, "content": "private wrapper",
        },
    }) + "\n")
    comms.registry.declare(Thread('worker', frozenset(), str(tmp_path), session_file=RegistrySessionLocation.from_path(str(path), comms.root)))
    comms.transcripts.routes.record_input_display(SECOND_INPUT_ID, NoDisplayText())
    with closing(sqlite3.connect(comms.transcripts.routes.path)) as connection:
        assert connection.execute("PRAGMA journal_mode=" + journal_mode).fetchone()[0] == journal_mode.lower()
        captured = comms.transcripts.capture_page_read("worker")
        assert captured.read().events[0].declared_name == "user"
        original = path.read_bytes()
        comms.transcripts.routes.record_input_display(native_id, NoDisplayText())
        assert path.read_bytes() == original
        assert not captured.current()
        with pytest.raises(StaleRevision):
            captured.read()
        assert comms.transcripts.capture_page_read("worker").read().events[0].declared_name == "context"


def test_native_append_preserves_captured_cut_and_fork_source_change_revokes_it(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    parent = tmp_path / "parent.jsonl"
    parent.write_text(json.dumps({"type": "message", "id": "parent-1", "timestamp": "2026-10-19T00:00:00Z", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "parent answer"}],
    }}) + "\n")
    comms.registry.declare(Thread('parent', frozenset(), str(tmp_path), session_file=RegistrySessionLocation.from_path(str(parent), comms.root)))
    comms.registry.declare(Thread('child', frozenset(), str(tmp_path), parent=ForkedParent('parent'), task=GivenThreadText('continue')))
    captured = comms.transcripts.capture_page_read("child")
    assert any(event.text == "parent answer" for event in captured.read().events)
    with parent.open("a") as output:
        output.write(json.dumps({"type": "message", "id": "parent-2", "timestamp": "2026-10-19T00:00:01Z", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "later answer"}],
        }}) + "\n")
    assert not captured.current()
    assert captured.content_current()
    captured_answers = [
        event.text for event in captured.read().events if event.declared_name == "assistant"
    ]
    assert captured_answers == ["parent answer"]
    fresh = comms.transcripts.capture_page_read("child")
    assert any(event.text == "later answer" for event in fresh.read().events)
    child = tmp_path / "child.jsonl"
    child.write_text(json.dumps({"type": "message", "id": "child-1", "timestamp": "2026-10-19T00:00:00Z", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "child answer"}],
    }}) + "\n")
    comms.threads.attach_session((attachment_owner := comms.registry.require("child")), str(child), process=attachment_owner.process_identity)
    with pytest.raises(StaleRevision):
        fresh.read()
    assert [event.text for event in comms.transcripts.capture_page_read("child").read().events] == ["child answer"]


def test_native_rewrite_inside_captured_cut_revokes_the_read(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    path = tmp_path / "native.jsonl"
    original = json.dumps({"type": "message", "id": "entry", "timestamp": "2026-10-19T00:00:00Z", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "first answer"}],
    }}) + "\n"
    replacement = original.replace("first answer", "other answer")
    assert len(replacement) == len(original)
    path.write_text(original)
    comms.registry.declare(Thread('worker', frozenset(), str(tmp_path), session_file=RegistrySessionLocation.from_path(str(path), comms.root)))
    captured = comms.transcripts.capture_page_read("worker")

    path.write_text(replacement)

    assert not captured.content_current()
    with pytest.raises(StaleRevision):
        captured.read()


def test_thread_activity_and_annotations_do_not_replay_saved_content(tmp_path):
    from dataclasses import replace
    from agent_comms.acp_extension import TranscriptSnapshotUpdate

    comms = installed_comms(tmp_path / "wire")
    path = tmp_path / "native.jsonl"
    path.write_text(json.dumps({"type": "message", "id": "entry", "timestamp": "2026-10-19T00:00:00Z", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "Original retained answer"}],
    }}) + "\n")
    comms.registry.declare(Thread('worker', frozenset(), str(tmp_path), session_file=RegistrySessionLocation.from_path(str(path), comms.root)))
    captured = comms.transcripts.capture_page_read("worker")
    original = captured.read()
    comms.registry.heartbeat("worker")
    assert captured.current()
    comms.registry.register(replace(comms.registry.require("worker"), title=GivenThreadText("Updated annotation")))
    # The original source owner still fences native bytes, routes, publication
    # proof and incarnation. A heartbeat changes annotations, not those bytes.
    assert not captured.current()
    assert captured.read() == original
    fresh = comms.transcripts.capture_page_read("worker")
    assert fresh.identity.same_content(captured.identity)
    assert TranscriptSnapshotUpdate.capture(comms.transcripts, "worker").page == fresh.read()
