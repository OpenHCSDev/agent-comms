"""Edit clocks come from their native event or the original external SQL timestamp."""

import pytest

from agent_comms.commander.judge import Edit
from agent_comms.external_transcript import ExternalEventRow, ExternalTranscript
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.tool_calls import CliToolArguments, ToolCall
from agent_comms.turn_digests import UnjudgedEdits
from dataclasses import replace


def original_call():
    arguments = {'file_path': 'owner.py', 'old_string': 'before', 'new_string': 'after'}
    return CliToolArguments.decode(ToolCall('source-call', 'Edit', arguments), arguments)


def assert_edit(edit, clock):
    assert edit.id == 'source-call'
    assert edit.problem == 'P215'
    assert edit.hunks[0].old_text == 'before'
    assert edit.hunks[0].new_text == 'after'
    assert edit.at == clock
    assert '- before' in edit.rendered()
    assert '+ after' in edit.rendered()
    assert clock.clock() in edit.rendered()


def test_native_edit_keeps_its_recorded_time_through_reply_and_presentation():
    timestamp = 1792430000.0
    start, _ = original_call().finished('done', True, 12, timestamp)
    evidence = start.edit_calls('unit', 'P215', 1.0)
    wire = FieldCodec.encode(UnjudgedEdits(evidence))
    presented, = FieldCodec.decode(tuple[Edit, ...], wire['edits'])
    assert_edit(presented, RecordedEventTime(timestamp))
    assert presented.thread == 'unit'
    assert wire['edits'][0]['at'] == FieldCodec.encode(presented.at, EventTime)


def test_recorded_clock_requires_its_original_float():
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(EventTime, {'kind': 'recorded', 'at': None})
    with pytest.raises(ValueError, match='finite Unix timestamp'):
        RecordedEventTime(None)
