"""Canonical saved clocks survive projection, ACP transport and page rebuild."""

from dataclasses import replace
from datetime import datetime

import pytest

from agent_comms.field_codec import MalformedRecord, RecordedTimestamp
from agent_comms.native_entries import NativeEntry, TranscriptProjection
from agent_comms.routing import TurnRouting
from agent_comms.transcript_events import AssistantTranscript
from conftest import installed_comms


@pytest.mark.parametrize("stamp", ["2026-09-28T23:12:33.123Z", "2026-09-28T19:12:33.123-04:00"])
def test_original_record_clock_survives_split_and_routed_projection(tmp_path, stamp):
    comms = installed_comms(tmp_path / "wire")
    for name in ("peer", "owner"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    requests = tuple(
        comms.messaging.send("peer", "owner", text).message for text in ("first", "second")
    )
    entry = NativeEntry.from_wire(
        {
            "type": "message",
            "timestamp": stamp,
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "native prompt"},
                    {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
                ],
            },
        }
    )
    expected = datetime.fromisoformat(stamp).timestamp()
    for context in (TranscriptProjection(position=0), TranscriptProjection(position=0, routing=TurnRouting(requests))):
        events = entry.events(context)
        assert len(events) == 2
        assert all(event.timestamp == expected for event in events)
        assert entry.timestamp == RecordedTimestamp(stamp)  # Native external field stays exact.
    compacted = NativeEntry.from_wire(
        {"type": "compaction", "timestamp": stamp, "summary": "kept context"}
    )
    assert compacted.events(TranscriptProjection(position=0))[0].timestamp == expected

    first = AssistantTranscript("start", timestamp=expected, position=0)
    assert first.merge(replace(first, text="continued")).timestamp == expected
    assert first.merge(replace(first, timestamp=expected + 1)) is None


def test_new_native_entry_inherits_original_clock_projection():
    class ExternalNoteEntry(NativeEntry):
        def _events(self, context, timestamp):
            return [AssistantTranscript("a new external note", position=context.position, timestamp=timestamp)]

    # The inherited entry owner decodes the source clock before constructing any content.
    stamp = "2026-09-28T23:12:33.123Z"
    expected = datetime.fromisoformat(stamp).timestamp()
    note = ExternalNoteEntry(timestamp=RecordedTimestamp(stamp))
    assert note.events(TranscriptProjection(position=0))[0].timestamp == expected


@pytest.mark.parametrize("stamp", [None, "not-a-time", "2026-09-28T23:12:33"])
def test_an_entry_without_an_absolute_recorded_clock_is_refused(stamp):
    with pytest.raises(MalformedRecord):
        NativeEntry.from_wire(
            {"type": "message", "timestamp": stamp, "message": {"role": "user", "content": "preserved"}}
        )


@pytest.mark.parametrize('source', ('claude', 'codex'))
@pytest.mark.parametrize('stamp', (None, 'missing'))
def test_external_source_without_a_time_refuses_before_event_construction(source, stamp):
    from pydantic import ValidationError
    from agent_comms.cli_bridges.claude_protocol import TimedRecord
    from agent_comms.cli_bridges.codex_rollout import ROLLOUT_RECORD
    from agent_comms.recorded_time import UnrecordedTimeError

    record = ({'uuid': 'record', 'position': 0} if source == 'claude' else
              {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': []}})
    if stamp is None:
        record['timestamp'] = None
    decode = TimedRecord.model_validate if source == 'claude' else ROLLOUT_RECORD.validate_python
    with pytest.raises(ValidationError) as refused:
        decode(record)
    assert any(isinstance(error.get('ctx', {}).get('error'), UnrecordedTimeError)
               for error in refused.value.errors())


def test_codex_rollout_projects_the_enclosing_record_clock():
    from agent_comms.cli_bridges.codex_rollout import ROLLOUT_RECORD, RolloutState

    record = ROLLOUT_RECORD.validate_python({
        'type': 'response_item', 'timestamp': '2026-10-19T18:00:00Z',
        'payload': {'type': 'message', 'role': 'assistant', 'id': 'item',
                    'content': [{'text': 'saved source text'}]},
    })
    update, = record.updates(RolloutState(), 17)
    assert update.events[0].timestamp == datetime.fromisoformat('2026-10-19T18:00:00Z').timestamp()


def test_malformed_shell_words_refuse_instead_of_reaching_any_rule(tmp_path):
    from agent_comms.storage_rules import ShellInvocation

    with pytest.raises(ValueError):
        tuple(ShellInvocation.parse('uv pip install package "unterminated', tmp_path))
