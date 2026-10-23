from conftest import transcript_records
from agent_comms.thread_text import SavedSessionFile
from agent_comms.coordination_errors import TranscriptChanged
import json
from dataclasses import replace

import pytest

from conftest import installed_comms
from agent_comms.threads import Thread
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.transcripts import NativeTranscriptCursor, WireTranscriptCursor
from agent_comms.thread_text import ForkedParent, GivenThreadText


def transcript(path, count):
    path.write_text(
        json.dumps(transcript_records.session(id="session").to_wire())
        + "\n"
        + "".join(
            json.dumps(
                transcript_records.message({"role": "assistant", "content": [{"type": "text", "text": f"Record {i}"}]})
            )
            + "\n"
            for i in range(count)
        )
    )


def test_pages_reach_beginning_and_return_to_tail_without_duplicates(tmp_path):
    path = tmp_path / "session.jsonl"
    transcript(path, 105)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(tmp_path), session_file=SavedSessionFile(str(path))))
    page = comms.transcripts.thread_transcript_page("worker", max_messages=10)
    through = page.after
    seen = [event.text for event in page.events]
    assert seen == [f"Record {i}" for i in range(95, 105)]
    while page.has_older:
        page = comms.transcripts.thread_transcript_page(
            "worker", before=page.before, max_messages=10
        )
        seen[:0] = [event.text for event in page.events]
    assert seen == [f"Record {i}" for i in range(105)]
    assert page.before.offset == 0
    # New live output must not leak into the frozen replay window and duplicate ACP input.
    with path.open("a") as output:
        output.write(
            json.dumps(transcript_records.message({"role": "assistant", "content": [{"type": "text", "text": "LIVE"}]}))
            + "\n"
        )
    seen = [event.text for event in page.events]
    while page.has_newer:
        page = comms.transcripts.thread_transcript_page(
            "worker", after=page.after, through=through, max_messages=10
        )
        seen.extend(event.text for event in page.events)
    assert seen == [f"Record {i}" for i in range(105)]


def test_oversized_message_and_file_identity(tmp_path):
    path = tmp_path / "session.jsonl"
    text = "x" * 200000
    path.write_text(
        json.dumps(transcript_records.message({"role": "assistant", "content": [{"type": "text", "text": text}]})) + "\n"
    )
    comms = installed_comms(tmp_path / "wire")
    thread = comms.registry.new_thread('worker', frozenset(), str(tmp_path), session_file=SavedSessionFile(str(path)))
    comms.registry.declare(thread)
    page = comms.transcripts.thread_transcript_page("worker", max_bytes=100)
    assert page.events[0].text == text
    assert not page.has_older
    comms.registry.declare(replace(thread, session_file=SavedSessionFile(str(tmp_path / "other.jsonl"))))
    with pytest.raises(TranscriptChanged):
        comms.transcripts.thread_transcript_page("worker", before=page.before)
    with pytest.raises(ValueError):
        comms.transcripts.thread_transcript_page("worker", max_messages=0)
    with pytest.raises(TranscriptChanged):
        comms.transcripts.thread_transcript_page("worker", before=NativeTranscriptCursor(str(path), -1))


def test_empty_transcript_and_non_message_records(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(tmp_path)))
    page = comms.transcripts.thread_transcript_page("worker")
    assert not page.events and not page.has_older and not page.has_newer


def test_new_fork_projects_parent_history_and_instruction_until_own_session_exists(tmp_path):
    parent_path = tmp_path / "parent.jsonl"
    transcript(parent_path, 3)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(
        comms.registry.new_thread('parent', frozenset(), str(tmp_path), session_file=SavedSessionFile(str(parent_path)))
    )
    comms.registry.declare(
        comms.registry.new_thread('child', frozenset(), str(tmp_path), parent=ForkedParent('parent'), task=GivenThreadText('Inspect the renderer'))
    )

    page = comms.transcripts.thread_transcript_page("child")
    assert page.after.session_file == str(parent_path)
    assert [event.text for event in page.events] == [
        "Record 0",
        "Record 1",
        "Record 2",
        "Forked from @parent. This thread started with:",
        "Inspect the renderer",
    ]
    assert [event.declared_name for event in page.events[-2:]] == ["fork_origin", "fork_task"]
    assert [event.text for event in comms.transcripts.thread_transcript_page("child").events][-2:] == [
        "Forked from @parent. This thread started with:",
        "Inspect the renderer",
    ]

    child_path = tmp_path / "child.jsonl"
    transcript(child_path, 1)
    comms.threads.attach_session((attachment_owner := comms.registry.require("child")), str(child_path), process=attachment_owner.process_identity)
    assert [event.text for event in comms.transcripts.thread_transcript_page("child").events] == [
        "Record 0"
    ]


def test_inherited_scroll_window_survives_child_session_persistence(tmp_path):
    parent_path = tmp_path / "parent.jsonl"
    transcript(parent_path, 35)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(
        comms.registry.new_thread('parent', frozenset(), str(tmp_path), session_file=SavedSessionFile(str(parent_path)))
    )
    comms.registry.declare(
        comms.registry.new_thread('child', frozenset(), str(tmp_path), parent=ForkedParent('parent'), task=GivenThreadText('Child task'))
    )
    page = comms.transcripts.thread_transcript_page("child", max_messages=5)
    through = page.after
    own_path = tmp_path / "child.jsonl"
    transcript(own_path, 1)
    comms.threads.attach_session((attachment_owner := comms.registry.require("child")), str(own_path), process=attachment_owner.process_identity)
    seen = [event.text for event in page.events if event.declared_name == "assistant"]
    while page.has_older:
        page = comms.transcripts.thread_transcript_page(
            "child", before=page.before, through=through, max_messages=5
        )
        seen[:0] = [event.text for event in page.events if event.declared_name == "assistant"]
    assert seen == [f"Record {i}" for i in range(35)]
    assert comms.transcripts.thread_transcript_page("child").after.session_file == str(own_path)
    unrelated = tmp_path / "unrelated.jsonl"
    transcript(unrelated, 1)
    with pytest.raises(TranscriptChanged):
        comms.transcripts.thread_transcript_page(
            "child", through=NativeTranscriptCursor(str(unrelated), unrelated.stat().st_size)
        )


def test_wire_cursor_keeps_the_furthest_committed_native_prefix_within_it():
    from agent_comms.transcript_sources import AssignedSourceCursor

    wire_at = AssignedSourceCursor("root", RecordedThreadIncarnation("recipient", 1.0), 5).at
    cursor = WireTranscriptCursor("a.jsonl", 900, wire_at(5))
    committed = (WireTranscriptCursor("a.jsonl", 40, wire_at(2)), WireTranscriptCursor("a.jsonl", 70, wire_at(5)),
                 WireTranscriptCursor("a.jsonl", 99, wire_at(6)), WireTranscriptCursor("b.jsonl", 500, wire_at(1)))
    assert cursor.over_native(committed) == WireTranscriptCursor("a.jsonl", 70, wire_at(5))
    assert cursor.over_native(committed[3:]) == WireTranscriptCursor("b.jsonl", 500, wire_at(5))
    assert cursor.over_native(()) == WireTranscriptCursor("a.jsonl", 0, wire_at(5))


def test_never_started_native_child_keeps_no_native_cursor_and_no_read_mark(tmp_path):
    from agent_comms.cli_commands import RegisterCliCommand
    from agent_comms.field_codec import FieldCodec
    from agent_comms.thread_execution import NativeThreadExecution
    from agent_comms.transcripts import NoNativeTranscriptCursor, TranscriptCursor

    path = tmp_path / "parent.jsonl"
    transcript(path, 3)
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(comms.registry.new_thread(
        "parent", frozenset(), str(tmp_path), session_file=SavedSessionFile(str(path)),
        execution=NativeThreadExecution,
    ))
    RegisterCliCommand(name="child", worktree=str(tmp_path), parent=ForkedParent("parent"),
                       execution=NativeThreadExecution).apply(comms)
    checkpoint = comms.transcripts.transcript_checkpoint("child")
    inherited = comms.transcripts.thread_transcript_page("child").after
    painted = checkpoint.over_native((inherited,))
    assert painted == NoNativeTranscriptCursor(source=comms.registry.require("child").runtime,
                                               wire=checkpoint.wire)
    assert inherited == NoNativeTranscriptCursor(source=comms.registry.require("parent").runtime,
                                                 offset=path.stat().st_size, wire=checkpoint.wire)
    assert FieldCodec.decode(TranscriptCursor, FieldCodec.encode(painted)) == painted
    ledger = comms.bus.reads.path
    before = ledger.read_bytes() if ledger.exists() else b""
    comms.views.mark_thread_view_read("child", worktree=str(tmp_path), through=painted)
    comms.views.mark_thread_view_read("child", worktree=str(tmp_path), through=inherited)
    assert (ledger.read_bytes() if ledger.exists() else b"") == before

    parent = comms.transcripts.transcript_checkpoint("parent")
    unpainted = replace(checkpoint, wire=parent.wire)
    assert parent.over_native((unpainted,)) == unpainted
    comms.views.mark_thread_view_read("parent", worktree=str(tmp_path), through=unpainted)
    assert (ledger.read_bytes() if ledger.exists() else b"") == before
    comms.views.mark_thread_view_read("parent", worktree=str(tmp_path), through=parent)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    assert comms.bus.reads.transcript_seen(viewer, str(path), path.stat().st_ino) == path.stat().st_size

    # The same cursor state retains its own external record coordinate, not a parent's.
    own = replace(checkpoint, offset=30)
    assert own.over_native((replace(own, offset=12), inherited)) == replace(own, offset=12)
