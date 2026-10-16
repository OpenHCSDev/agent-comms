"""Shared tool-result contract; native entry selection belongs to its store."""
from dataclasses import replace

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.message_reference import MessageReference
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.tool_activity import (
    NativeToolSelectorUnavailable, NoToolActivity, RecordedToolActivity, ToolActivity,
)
from agent_comms.transcript_events import AgentTextTranscript, ToolEndTranscript, ToolStartTranscript


def original_call(timestamp=0):
    return ToolStartTranscript(tool_call_id='original', tool_name='bash',
                               raw_input={'command': 'read source'}, detail='original detail',
                               timestamp=timestamp, source=MessageReference(7, 'original-source'))


def test_only_starts_expose_original_calls_in_block_order():
    first = original_call()
    last = replace(first, tool_call_id='last')
    result = ToolEndTranscript(tool_call_id=first.tool_call_id, tool_name=first.tool_name,
                               text='completed', ok=True)
    events = (AgentTextTranscript('text'), first, result, last)
    calls = tuple(call for event in events for call in event.tool_calls)
    assert calls == (first, last)
    assert calls[0] is first and calls[-1] is last
    assert result.tool_calls == ()


def test_selected_record_preserves_all_original_facts_through_codec():
    call = original_call()
    activity = RecordedToolActivity(call)
    assert activity.call is call
    restored = FieldCodec.decode(ToolActivity, FieldCodec.encode(activity))
    assert restored == activity
    assert restored.call.raw_input == {'command': 'read source'}
    assert restored.call.source == MessageReference(7, 'original-source')
    assert restored.call.timestamp == 0
    assert restored.timestamps == (0,)


@pytest.mark.parametrize('timestamp,busy,expected', [
    (None, RecordedEventTime(0), False),
    (0, RecordedEventTime(0), True),
    (70, RecordedEventTime(0), True),
    (95, RecordedEventTime(0), False),
    (0, RecordedEventTime(95), False),
    (70, UnrecordedEventTime(), False),
    (None, UnrecordedEventTime(), False),
    (70, RecordedEventTime(110), False),
])
def test_silence_derives_both_original_clocks(timestamp, busy, expected):
    activity = RecordedToolActivity(original_call(timestamp))
    assert activity.timestamps == (() if timestamp is None else (timestamp,))
    assert activity.silent_since(busy, now=100, seconds=20) is expected


def test_proven_absence_is_not_an_unavailable_selector():
    busy = RecordedEventTime(0)
    assert NoToolActivity().silent_since(busy, 100, 20)
    assert not NativeToolSelectorUnavailable().silent_since(busy, 100, 20)
    assert not NoToolActivity().silent_since(UnrecordedEventTime(), 100, 20)
    for value in (NoToolActivity(), NativeToolSelectorUnavailable()):
        assert FieldCodec.decode(ToolActivity, FieldCodec.encode(value)) == value
