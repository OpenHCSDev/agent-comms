"""Tool-call times belong to each execution's original history, never the watcher."""

from agent_comms.execution.transcript_sources import ExternalTranscript
from agent_comms.execution.turn_item import (
    NativeItemId,
    NativeTurnId,
)
from agent_comms.nominal.field_codec import ToolCallId
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.storage.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.transcript.transcript_events import AssistantTranscript, ToolStartTranscript


def test_newest_tool_call_time_and_comparisons_cross_midnight(tmp_path):
    turn = NativeTurnId('turn')
    transcript = ExternalTranscript(tmp_path, RecordedThreadIncarnation("worker", 1.0))
    assert transcript.last_tool_call() == UnrecordedEventTime()
    assert not transcript.path.exists()
    transcript.finish(turn_id=turn, item_id=NativeItemId("first"), events=(
        ToolStartTranscript(position=0, tool_call_id=ToolCallId("first"), tool_name="bash", timestamp=86390.0),))
    transcript.finish(turn_id=turn, item_id=NativeItemId("text"),
        events=(AssistantTranscript("newer text", position=0, timestamp=86420.0),))
    assert transcript.last_tool_call() == RecordedEventTime(86390.0)
    transcript.finish(turn_id=turn, item_id=NativeItemId("second"), events=(
        ToolStartTranscript(position=0, tool_call_id=ToolCallId("second"), tool_name="bash", timestamp=86410.0),))
    latest = transcript.last_tool_call()
    assert latest.since(86399.0)
    assert not latest.before(86400.0)
