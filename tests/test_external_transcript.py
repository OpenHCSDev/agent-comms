import pytest

from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.threads import Thread
from agent_comms.transcript_events import AssistantTranscript, NoticeTranscript
from agent_comms.transcripts import TranscriptReadIdentity


def test_finished_cli_items_append_once_and_page(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread("codex", frozenset(), str(tmp_path)))
    transcript = comms.transcripts.external("codex")
    transcript.finish(turn_id="t1", item_id="compaction",
                      events=(NoticeTranscript("Compacting context…", timestamp=2.0),))
    read = comms.transcripts.capture_page_read("codex")
    assert read.content_current()
    # The identity crosses the ACP and observation wires.
    wired = FieldCodec.decode(TranscriptReadIdentity, FieldCodec.encode(read.identity))
    assert wired == read.identity
    assert [event.text for event in comms.transcripts.thread_transcript_page("codex").events] \
        == ["Compacting context…"]

    # Finishing appends after what the read captured; the read stays current.
    transcript.finish(turn_id="t1", item_id="reply",
                      events=(AssistantTranscript("Hello", timestamp=1.0),))
    # A watch that starts again republishes what Codex already finished.
    transcript.finish(turn_id="t1", item_id="reply",
                      events=(AssistantTranscript("Hello", timestamp=1.0),))
    transcript.finish(turn_id="t1", item_id="hook", events=())
    assert read.content_current()
    page = comms.transcripts.thread_transcript_page("codex")
    assert [event.text for event in page.events] == ["Compacting context…", "Hello"]

    newest = comms.transcripts.thread_transcript_page("codex", max_messages=1)
    assert [event.text for event in newest.events] == ["Hello"]
    older = comms.transcripts.thread_transcript_page("codex", before=newest.before,
                                                     max_messages=1)
    assert [event.text for event in older.events] == ["Compacting context…"]
    assert newest.has_older and not older.has_older
    later = comms.transcripts.thread_transcript_page("codex", after=older.after)
    assert [event.text for event in later.events] == ["Hello"]


def test_a_native_session_thread_takes_no_cli_items(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread("pi", frozenset(), str(tmp_path),
                                  session_file=str(tmp_path / "session.jsonl")))
    with pytest.raises(ValueError, match="native session"):
        comms.transcripts.external("pi")
    assert comms.transcripts.thread_draft("pi") is None


def test_draft_is_current_state_and_finishes_atomically(tmp_path):
    from agent_comms.acp_extension import TranscriptSnapshotUpdate, encode_updates, decode_updates
    from agent_comms.coordination_errors import StaleRevision
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread("codex", frozenset(), str(tmp_path)))
    transcript = comms.transcripts.external("codex")
    transcript.draft(turn_id="t", item_id="a", events=(AssistantTranscript("Hel"),))
    assert comms.transcripts.thread_draft("codex").events[0].text == "Hel"
    assert transcript.cut().offset == 0
    transcript.draft(turn_id="t", item_id="a", events=(AssistantTranscript("Hello"),))
    transcript.finish(turn_id="t", item_id="other", events=(NoticeTranscript("Tool"),))
    assert transcript.current_draft().item_id == "a"
    read = comms.transcripts.capture_page_read("codex")
    snapshot = TranscriptSnapshotUpdate.from_read(read)
    assert snapshot.draft.events[0].text == "Hello"
    assert [e.text for e in snapshot.page.events] == ["Tool"]
    assert decode_updates(encode_updates(snapshot)) == (snapshot,)
    transcript.finish(turn_id="t", item_id="a", events=(AssistantTranscript("Hello!"),))
    with pytest.raises(StaleRevision):
        read.current_draft()
    assert transcript.current_draft() is None
    assert [e.text for e in comms.transcripts.thread_transcript_page("codex").events] == ["Tool", "Hello!"]
    transcript.draft(turn_id="t2", item_id="b", events=(AssistantTranscript("Interrupted"),))
    transcript.end_turn()
    assert transcript.current_draft() is None
