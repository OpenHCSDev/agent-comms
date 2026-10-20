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


@pytest.mark.parametrize('unrecorded', [False, True])
def test_external_edit_clock_survives_actual_unjudged_tool(commander, unrecorded):
    from test_commander_problem_orders import create

    comms, _ = commander
    todo = create(commander, 'P215')
    holder = comms.registry.require(todo.held().owner)
    comms.registry.register(replace(holder, execution=ExternalThreadExecution))
    timestamp = todo.order.at.recorded_at('An order') + 1.0
    start, end = original_call().finished('done', True, 12, timestamp)
    transcript = ExternalTranscript(comms.root, holder.incarnation)
    transcript.finish(turn_id='original-turn', item_id='source-call', events=(start, end))
    if unrecorded:
        # Reproduce the old publisher's wire shape only in this test-owned database.
        with transcript.writing() as connection:
            connection.execute(
                f'UPDATE "{ExternalEventRow.declared_name}" '
                "SET event=json_set(event, '$.timestamp', NULL) WHERE thread=?",
                (transcript.lookup,))
    edit, = Edit.unjudged(comms, 'lead')
    clock = UnrecordedEventTime() if unrecorded else RecordedEventTime(timestamp)
    assert_edit(edit, clock)
    assert edit.thread == holder.name
    with transcript.reading() as records:
        assert records.unjudged_count(todo) == 1


def test_recorded_clock_requires_its_original_float():
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(EventTime, {'kind': 'recorded', 'at': None})
    with pytest.raises(ValueError, match='finite Unix timestamp'):
        RecordedEventTime(None)
