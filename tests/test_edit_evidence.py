"""Source sanity only; copied-bus rotation is the application check."""

from dataclasses import fields, replace

import pytest

from agent_comms.commander.judge import RecordedEdit, UnrecordedEdit
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.tool_calls import CliToolArguments, ToolCall
from agent_comms.tool_declaration import EditCall, UnrecordedEditCall
from agent_comms.turn_digests import UnjudgedEdits


def original_call():
    arguments = {'file_path': 'owner.py', 'old_string': 'before', 'new_string': 'after'}
    return CliToolArguments.decode(ToolCall('source-call', 'Edit', arguments), arguments)


@pytest.mark.parametrize('timestamp', [None, 1792430000.0])
def test_original_call_keeps_all_edit_facts_through_reply_and_presentation(timestamp):
    start, _ = original_call().finished('done', True, 12)
    start = replace(start, timestamp=timestamp)
    evidence = start.edit_calls('unit', 'P215', 1.0)
    wire = FieldCodec.encode(UnjudgedEdits(evidence))
    presented, = FieldCodec.decode(tuple[RecordedEdit | UnrecordedEdit, ...], wire['edits'])
    assert presented.id == 'source-call'
    assert presented.thread == 'unit'
    assert presented.problem == 'P215'
    assert presented.hunks[0].old_text == 'before'
    assert presented.hunks[0].new_text == 'after'
    assert '- before' in presented.rendered()
    assert '+ after' in presented.rendered()
    if timestamp is None:
        assert isinstance(evidence[0], UnrecordedEditCall)
        assert isinstance(presented, UnrecordedEdit)
        assert 'at' not in {field.name for field in fields(presented)}
        assert 'at' not in wire['edits'][0]
        assert presented.clock() == '-'
        assert presented.dated == ()
    else:
        assert isinstance(evidence[0], EditCall)
        assert isinstance(presented, RecordedEdit)
        assert presented.at == timestamp
        assert presented.dated == (timestamp,)
        assert type(wire['edits'][0]['at']) is float


def test_null_float_is_rejected_not_reinterpreted_as_undated():
    start, _ = original_call().finished('done', True, 12)
    wire = FieldCodec.encode(UnjudgedEdits(start.edit_calls('unit', 'P215', 1.0)))
    wire['edits'][0]['at'] = None
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(UnjudgedEdits, wire)


def test_recorded_constructor_refuses_missing_clock():
    with pytest.raises(ValueError, match='finite Unix timestamp'):
        EditCall('id', 'unit', 'owner.py', (), 'P215', None)
