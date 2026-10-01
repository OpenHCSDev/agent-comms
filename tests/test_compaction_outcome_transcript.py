"""Original journal outcomes advance source evidence without rewriting native history."""


import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from agent_comms.retained_task_facts import RetainedTaskFacts
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import SelectedSummaryAttempt, SelectedSummarySource
from agent_comms.compaction_states import RefusedSummary, ReservedSummary, UnknownSummary
from agent_comms.coordination_errors import StaleRevision
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_compaction_settings import PiCompactionSettings
from agent_comms.pi_summary_payloads import SelectedModel
from agent_comms.selected_source import ManualSource, SessionRevision
from agent_comms.thread_identity import TurnId
from agent_comms.threads import Thread


def test_fresh_transcript_boundary_decodes_outcome_without_journal_reader():
    from agent_comms.compaction_identity import SummaryOperationIdentity
    from agent_comms.transcript_events import CompactionOutcomeTranscript

    original = CompactionOutcomeTranscript(
        text="Original uncertain compaction", identity=SummaryOperationIdentity("/original/session", "a" * 32),
    )
    code = """
import json, sys
from agent_comms.acp_extension import decode_updates
from agent_comms.field_codec import FieldCodec
from agent_comms.transcript_events import TranscriptEvent
assert 'agent_comms.compaction_outcomes' not in sys.modules
event = FieldCodec.decode(TranscriptEvent, json.loads(sys.argv[1]))
assert event.identity.operation_id == 'a' * 32
assert event.timestamp is None
assert 'agent_comms.compaction_outcomes' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code, json.dumps(FieldCodec.encode(original))], check=True)


def test_late_original_outcome_invalidates_source_and_pages_once(tmp_path):
    comms = Comms(tmp_path / "wire")
    native = tmp_path / "session.jsonl"
    native.write_text("".join(json.dumps({
        "type": "message", "id": f"native-{index}",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "equal original bodies"}
        ]},
    }) + "\n" for index in range(3)))
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path), session_file=str(native)))
    captured = comms.transcripts.capture_page_read("worker")
    journal_path = comms.root / "compaction-commits.sqlite3"
    assert not journal_path.exists(), "A transcript read initialized a journal"
    journal = CompactionJournal(journal_path)
    source = SelectedSummarySource(
        ManualSource(owner=ProcessIdentity.capture(os.getpid()),
                     incarnation=comms.registry.require("worker").incarnation,
                     turn=TurnId("original-manual-turn"), reserved_revision=SessionRevision.observe(str(native)).require_available()),
        SelectedModel("controlled", "fixture", 272000), PiCompactionSettings(16384, 20000),
        RetainedTaskFacts(()),
    )
    original = SelectedSummaryAttempt("a" * 32, str(native),
                                     json.dumps(FieldCodec.encode(source)), ReservedSummary())
    later = replace(original, operation_id="b" * 32,
                    state=RefusedSummary("original bounded refusal"))
    with journal.transaction() as db:
        original.insert(db)
        later.insert(db)
    before = comms.transcripts.capture_page_read("worker")
    before_page = before.read()
    assert not captured.content_current()
    original_bytes = native.read_bytes()
    with journal.transaction() as db:
        original.transition(db, UnknownSummary())
    assert native.read_bytes() == original_bytes
    assert not before.content_current()
    with pytest.raises(StaleRevision):
        before.read()
    page = comms.transcripts.capture_page_read("worker").read()
    assert page.after.outcome_seq == before_page.after.outcome_seq
    assert not before_page.after.contains(page.after)
    assert not page.after.contains(before_page.after)
    from agent_comms.transcript_events import CompactionOutcomeTranscript
    outcomes = [event for event in page.events if isinstance(event, CompactionOutcomeTranscript)]
    assert [event.identity for event in outcomes] == [original.identity, later.identity]
    assert all(event.timestamp is None for event in outcomes)
    # Native IDs and bodies are not deduplicated to make the journal path work.
    assert sum(event.text == "equal original bodies" for event in page.events) == 3
    tail = comms.transcripts.thread_transcript_page("worker", max_messages=1)
    through = tail.after
    events = list(tail.events)
    while tail.has_older:
        tail = comms.transcripts.thread_transcript_page(
            "worker", before=tail.before, through=through, max_messages=1,
        )
        events[:0] = tail.events
    assert tuple(events) == page.events
    forward = list(tail.events)
    while tail.has_newer:
        tail = comms.transcripts.thread_transcript_page(
            "worker", after=tail.after, through=through, max_messages=1,
        )
        forward.extend(tail.events)
    assert tuple(forward) == page.events
