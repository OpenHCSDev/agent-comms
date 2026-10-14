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
