import json
from dataclasses import replace

import pytest

from agent_comms import Thread, TranscriptCursor, wire


def transcript(path, count):
    path.write_text(
        json.dumps({"type": "session", "id": "session"})
        + "\n"
        + "".join(
            json.dumps(
                {"type": "message", "message": {"role": "assistant", "content": f"Record {i}"}}
            )
            + "\n"
            for i in range(count)
        )
    )


def test_pages_reach_beginning_and_return_to_tail_without_duplicates(tmp_path):
    path = tmp_path / "session.jsonl"
    transcript(path, 105)
    comms = wire(tmp_path / "wire")
    comms.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(path)))
    page = comms.thread_transcript_page("worker", max_messages=10)
    through = page.after
    seen = [event.text for event in page.events]
    assert seen == [f"Record {i}" for i in range(95, 105)]
    while page.has_older:
        page = comms.thread_transcript_page("worker", before=page.before, max_messages=10)
        seen[:0] = [event.text for event in page.events]
    assert seen == [f"Record {i}" for i in range(105)]
    assert page.before.offset == 0
    # New live output must not leak into the frozen replay window and duplicate ACP input.
    with path.open("a") as output:
        output.write(
            json.dumps({"type": "message", "message": {"role": "assistant", "content": "LIVE"}})
            + "\n"
        )
    seen = [event.text for event in page.events]
    while page.has_newer:
        page = comms.thread_transcript_page(
            "worker", after=page.after, through=through, max_messages=10
        )
        seen.extend(event.text for event in page.events)
    assert seen == [f"Record {i}" for i in range(105)]


def test_oversized_message_and_file_identity(tmp_path):
    path = tmp_path / "session.jsonl"
    text = "x" * 200000
    path.write_text(
        json.dumps({"type": "message", "message": {"role": "assistant", "content": text}}) + "\n"
    )
    comms = wire(tmp_path / "wire")
    thread = Thread("worker", frozenset(), str(tmp_path), session_file=str(path))
    comms.register(thread)
    page = comms.thread_transcript_page("worker", max_bytes=100)
    assert page.events[0].text == text
    assert not page.has_older
    comms.register(replace(thread, session_file=str(tmp_path / "other.jsonl")))
    with pytest.raises(ValueError, match="changed"):
        comms.thread_transcript_page("worker", before=page.before)
    with pytest.raises(ValueError):
        comms.thread_transcript_page("worker", max_messages=0)
    with pytest.raises(ValueError):
        comms.thread_transcript_page("worker", before=TranscriptCursor(str(path), -1))


def test_empty_transcript_and_non_message_records(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.register(Thread("worker", frozenset(), str(tmp_path)))
    page = comms.thread_transcript_page("worker")
    assert not page.events and not page.has_older and not page.has_newer


def test_new_fork_projects_parent_history_and_instruction_until_own_session_exists(tmp_path):
    parent_path = tmp_path / "parent.jsonl"
    transcript(parent_path, 3)
    comms = wire(tmp_path / "wire")
    comms.register(Thread("parent", frozenset(), str(tmp_path), session_file=str(parent_path)))
    comms.register(
        Thread("child", frozenset(), str(tmp_path), parent="parent", task="Inspect the renderer")
    )

    page = comms.thread_transcript_page("child")
    assert page.after.session_file == str(parent_path)
    assert [event.text for event in page.events] == [
        "Record 0",
        "Record 1",
        "Record 2",
        "Forked from @parent. This thread started with:",
        "Inspect the renderer",
    ]
    assert [event.kind for event in page.events[-2:]] == ["notice", "user"]
    assert [event.text for event in comms.thread_transcript("child")][-2:] == [
        "Forked from @parent. This thread started with:",
        "Inspect the renderer",
    ]

    child_path = tmp_path / "child.jsonl"
    transcript(child_path, 1)
    comms.attach_session("child", str(child_path))
    assert [event.text for event in comms.thread_transcript_page("child").events] == ["Record 0"]


def test_inherited_scroll_window_survives_child_session_persistence(tmp_path):
    parent_path = tmp_path / "parent.jsonl"
    transcript(parent_path, 35)
    comms = wire(tmp_path / "wire")
    comms.register(Thread("parent", frozenset(), str(tmp_path), session_file=str(parent_path)))
    comms.register(Thread("child", frozenset(), str(tmp_path), parent="parent", task="Child task"))
    page = comms.thread_transcript_page("child", max_messages=5)
    through = page.after
    own_path = tmp_path / "child.jsonl"
    transcript(own_path, 1)
    comms.attach_session("child", str(own_path))
    seen = [event.text for event in page.events if event.kind == "assistant"]
    while page.has_older:
        page = comms.thread_transcript_page(
            "child", before=page.before, through=through, max_messages=5
        )
        seen[:0] = [event.text for event in page.events if event.kind == "assistant"]
    assert seen == [f"Record {i}" for i in range(35)]
    assert comms.thread_transcript_page("child").after.session_file == str(own_path)
    unrelated = tmp_path / "unrelated.jsonl"
    transcript(unrelated, 1)
    with pytest.raises(ValueError, match="changed"):
        comms.thread_transcript_page(
            "child", through=TranscriptCursor(str(unrelated), unrelated.stat().st_size)
        )
