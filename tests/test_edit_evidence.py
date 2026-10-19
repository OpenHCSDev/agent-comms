"""Source checks for edit clocks owned by the existing EventTime family."""

from dataclasses import replace

import pytest

from agent_comms.commander.judge import Edit
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from agent_comms.tool_calls import CliToolArguments, ToolCall
from agent_comms.turn_digests import UnjudgedEdits


def original_call():
    arguments = {'file_path': 'owner.py', 'old_string': 'before', 'new_string': 'after'}
    return CliToolArguments.decode(ToolCall('source-call', 'Edit', arguments), arguments)


@pytest.mark.parametrize('timestamp,clock', [(None, UnrecordedEventTime()), (1792430000.0, RecordedEventTime(1792430000.0))])
def test_original_call_keeps_all_edit_facts_through_reply_and_presentation(timestamp, clock):
    start, _ = original_call().finished('done', True, 12)
    start = replace(start, timestamp=timestamp)
    evidence = start.edit_calls('unit', 'P215', 1.0)
    wire = FieldCodec.encode(UnjudgedEdits(evidence))
    presented, = FieldCodec.decode(tuple[Edit, ...], wire['edits'])
    assert presented.id == 'source-call'
    assert presented.thread == 'unit'
    assert presented.problem == 'P215'
    assert presented.hunks[0].old_text == 'before'
    assert presented.hunks[0].new_text == 'after'
    assert '- before' in presented.rendered()
    assert '+ after' in presented.rendered()
    assert presented.at == clock
    assert presented.at.clock() in presented.rendered()
    assert wire['edits'][0]['at'] == FieldCodec.encode(clock, EventTime)


def test_unrecorded_edit_survives_actual_commanders_unjudged_tool(commander):
    from agent_comms.external_transcript import ExternalTranscript
    from agent_comms.thread_execution import ExternalThreadExecution
    from test_commander_problem_orders import create

    comms, _ = commander
    todo = create(commander, 'P215')
    holder = comms.registry.require(todo.held().owner)
    comms.registry.register(replace(holder, execution=ExternalThreadExecution))
    start, end = original_call().finished('done', True, 12)
    ExternalTranscript(comms.root, holder.incarnation).finish(
        turn_id='original-turn', item_id='source-call', events=(start, end))
    edit, = Edit.unjudged(comms, 'lead')
    assert edit.id == 'source-call'
    assert edit.at == UnrecordedEventTime()
    assert edit.hunks[0].new_text == 'after'


def test_recorded_clock_requires_its_original_float():
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(EventTime, {'kind': 'recorded', 'at': None})
    with pytest.raises(ValueError, match='finite Unix timestamp'):
        RecordedEventTime(None)
