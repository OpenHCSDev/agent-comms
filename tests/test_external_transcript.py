import asyncio

import pytest

from agent_comms.bus import Bus
from agent_comms.session_file import RegistrySessionLocation
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.transcript_events import AssistantTranscript, NoticeTranscript
from agent_comms.transcripts import TranscriptReadIdentity, UnreadTranscriptCursor
from agent_comms.turn_digests import LastTurns
from agent_comms.threads import Thread
from agent_comms.turn_item import NativeItemId, NativeTurnId


@pytest.fixture(autouse=True)
def installed_bus(tmp_path):
    """Install the base bus; the external transcript store remains lazy."""
    Bus.install(Bus.path_in(tmp_path / "wire"))


def test_finished_cli_items_append_once_and_page(tmp_path):
    t1_turn_id = NativeTurnId('t1')
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread('codex', frozenset(), str(tmp_path)))
    transcript = comms.transcripts.external("codex")
    transcript.finish(
        turn_id=t1_turn_id,
        item_id=NativeItemId("compaction"),
        events=(
            NoticeTranscript(
                "Compacting context…", timestamp=2.0, position=NativeItemId("compaction")
            ),
        ),
    )
    read = comms.transcripts.capture_page_read("codex")
    assert read.content_current()
    # The identity crosses the ACP and observation wires.
    wired = FieldCodec.decode(TranscriptReadIdentity, FieldCodec.encode(read.identity))
    assert wired == read.identity
    assert [event.text for event in comms.transcripts.thread_transcript_page("codex").events] \
        == ["Compacting context…"]

    # Finishing appends after what the read captured; the read stays current.
    transcript.finish(
        turn_id=t1_turn_id,
        item_id=NativeItemId("reply"),
        events=(AssistantTranscript("Hello", timestamp=1.0, position=NativeItemId("reply")),),
    )
    # A watch that starts again republishes what Codex already finished.
    transcript.finish(
        turn_id=t1_turn_id,
        item_id=NativeItemId("reply"),
        events=(AssistantTranscript("Hello", timestamp=1.0, position=NativeItemId("reply")),),
    )
    transcript.finish(turn_id=t1_turn_id, item_id=NativeItemId("hook"), events=())
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


def test_absent_store_reads_stay_empty_without_creating_storage(tmp_path):
    comms = wire(tmp_path / 'wire')
    comms.registry.declare(Thread('cli', frozenset(), str(tmp_path)))
    transcript = comms.transcripts.external('cli')
    with transcript.reading() as read:
        assert not read.has_records(0)
        assert read.valid_offset(0, 0) and not read.valid_offset(-1, 0)
        assert read.start == 0 and read.cut().offset == 0
        assert not read.has_older(0) and not read.request_earlier(0)
        assert list(read.reverse(0)) == list(read.forward(0, 0)) == []
        assert read.turns(LastTurns(2)) == read.frames() == ()
        assert read.current_draft(transcript.root, transcript.incarnation).external_row() is None
    transcript.end_turn()
    assert not transcript.path.exists()


def test_captured_draft_cannot_delete_a_later_turns_draft(tmp_path):
    old_turn_id = NativeTurnId('old')
    new_turn_id = NativeTurnId('new')
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread("cli", frozenset(), str(tmp_path)))
    transcript = comms.transcripts.external("cli")
    transcript.draft(
        turn_id=old_turn_id,
        item_id=NativeItemId("draft"),
        events=(AssistantTranscript("old", timestamp=1.0, position=NativeItemId("draft")),),
    )
    with transcript.reading() as records:
        old = records.current_draft(transcript.root, transcript.incarnation)
    transcript.draft(
        turn_id=new_turn_id,
        item_id=NativeItemId("draft"),
        events=(AssistantTranscript("new", timestamp=1.0, position=NativeItemId("draft")),),
    )
    old.end_turn(transcript)
    with transcript.reading() as records:
        assert records.current_draft(transcript.root, transcript.incarnation).turn_id == "new"


@pytest.mark.asyncio
async def test_turn_owned_draft_commit_reaches_a_watching_reader(tmp_path):
    turn_turn_id = NativeTurnId('turn')
    from agent_comms.external_transcript import TurnDraftPublication
    from agent_comms.watched import ChangeSignal, FileSource

    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread('cli', frozenset(), str(tmp_path)))
    transcript = comms.transcripts.external('cli')
    marker, = transcript.source_paths(comms.root)
    async with ChangeSignal.opened(FileSource(marker)) as watcher:
        watcher.prepare()
        with transcript.writer_connection(check_same_thread=False) as connection:
            publication = TurnDraftPublication(transcript, connection, turn_turn_id)
            publication.begin_message(1.0)
            waiting = asyncio.create_task(watcher.wait())
            publication.publish(('Streaming',))
            await asyncio.wait_for(waiting, 2)
            with transcript.reading() as reader:
                draft = reader.current_draft(transcript.root, transcript.incarnation)
            assert draft.events[0].text == 'Streaming'
            publication.retire_message()


@pytest.mark.asyncio
async def test_earlier_page_requests_reach_initial_and_committed_watch_observations(tmp_path):
    turn_turn_id = NativeTurnId('turn')
    from agent_comms.cli_protocol import EarlierItems
    from agent_comms.external_transcript import EarlierPage
    from agent_comms.runtime_updates import FinishedItem

    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread("cli", frozenset(), str(tmp_path)))
    transcript = comms.transcripts.external("cli")
    transcript.publish_page(
        (FinishedItem(turn_turn_id, NativeItemId("newest"), ()),), EarlierPage("first")
    )
    emitted = asyncio.Queue()
    watching = asyncio.create_task(transcript.watch_requests(emitted.put_nowait))
    try:
        with transcript.reading() as records:
            assert records.request_earlier(0)
        first = await asyncio.wait_for(emitted.get(), 2)
        assert first.earlier.cursor == "first"
        transcript.prepend(
            EarlierItems.from_row(first),
            (FinishedItem(turn_turn_id, NativeItemId("older"), ()),), EarlierPage("second")
        )
        with transcript.reading() as records:
            assert records.request_earlier(-1)
        second = await asyncio.wait_for(emitted.get(), 2)
        assert second.earlier.cursor == "second" and second.seq < first.seq
    finally:
        watching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watching


def test_a_native_session_thread_takes_no_cli_items(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread('pi', frozenset(), str(tmp_path), session_file=RegistrySessionLocation.from_path(str(tmp_path / 'session.jsonl'), comms.root)))
    with pytest.raises(ValueError):
        comms.transcripts.external("pi")


def test_draft_is_current_state_and_finishes_atomically(tmp_path):
    t_turn_id = NativeTurnId('t')
    t2_turn_id = NativeTurnId('t2')
    from agent_comms.acp_extension import TranscriptSnapshotUpdate, encode_updates, decode_updates
    from agent_comms.coordination_errors import StaleRevision
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread('codex', frozenset(), str(tmp_path)))
    transcript = comms.transcripts.external("codex")
    transcript.draft(
        turn_id=t_turn_id,
        item_id=NativeItemId("a"),
        events=(AssistantTranscript("Hel", timestamp=1.0, position=NativeItemId("a")),),
    )
    with transcript.reading() as records:
        assert records.current_draft(transcript.root, transcript.incarnation).events[0].text == "Hel"
        assert records.cut().offset == 0
    transcript.draft(
        turn_id=t_turn_id,
        item_id=NativeItemId("a"),
        events=(AssistantTranscript("Hello", timestamp=1.0, position=NativeItemId("a")),),
    )
    transcript.finish(
        turn_id=t_turn_id,
        item_id=NativeItemId("other"),
        events=(NoticeTranscript("Tool", timestamp=1.0, position=NativeItemId("other")),),
    )
    with transcript.reading() as records:
        assert records.current_draft(transcript.root, transcript.incarnation).item_id == "a"
    read = comms.transcripts.capture_page_read("codex")
    snapshot = TranscriptSnapshotUpdate.from_read(read)
    assert snapshot.draft.events[0].text == "Hello"
    assert [e.text for e in snapshot.page.events] == ["Tool"]
    assert decode_updates(encode_updates(snapshot)) == (snapshot,)
    transcript.finish(
        turn_id=t_turn_id,
        item_id=NativeItemId("a"),
        events=(AssistantTranscript("Hello!", timestamp=1.0, position=NativeItemId("a")),),
    )
    with pytest.raises(StaleRevision):
        read.current_draft()
    with transcript.reading() as records:
        assert records.current_draft(transcript.root, transcript.incarnation).external_row() is None
    assert [e.text for e in comms.transcripts.thread_transcript_page("codex").events] == [
        "Tool",
        "Hello!",
    ]
    transcript.draft(
        turn_id=t2_turn_id,
        item_id=NativeItemId("b"),
        events=(AssistantTranscript("Interrupted", timestamp=1.0, position=NativeItemId("b")),),
    )
    transcript.end_turn()
    with transcript.reading() as records:
        assert records.current_draft(transcript.root, transcript.incarnation).external_row() is None


def test_a_thread_with_nothing_read_pages_older_and_newer_from_its_cursor(tmp_path):
    t1_turn_id = NativeTurnId('t1')
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread('codex', frozenset(), str(tmp_path)))
    transcript = comms.transcripts.external("codex")
    for index, text in enumerate(("first", "second", "third")):
        transcript.finish(
            turn_id=t1_turn_id,
            item_id=NativeItemId(text),
            events=(
                AssistantTranscript(
                    text,
                    timestamp=float(index),
                    position=FieldCodec.decode(int | NativeItemId, text),
                ),
            ),
        )

    async def loader(**bounds):
        return comms.transcripts.thread_transcript_page("codex", max_messages=1, **bounds)

    unread = UnreadTranscriptCursor()
    newest = asyncio.run(unread.read_page(loader))
    older = asyncio.run(unread.read_page(loader, before=newest.before))
    later = asyncio.run(unread.read_page(loader, after=older.after))
    # An unread page's own edges are not bounds: an agent tab opened before any
    # read still gets the newest page instead of a refused source.
    assert asyncio.run(unread.read_page(loader, before=unread)).events == newest.events
    assert asyncio.run(newest.after.read_page(loader, before=unread)).events == newest.events
    assert [event.text for event in newest.events] == ["third"]
    assert [event.text for event in older.events] == ["second"]
    assert [event.text for event in later.events] == ["third"]
