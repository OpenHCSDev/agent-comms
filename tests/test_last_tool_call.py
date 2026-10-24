"""Tool-call times belong to each execution's original history, never the watcher."""


from agent_comms.external_transcript import ExternalEventRow, ExternalTranscript
from agent_comms.native_tool_call import ToolCallId
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.transcript_events import AssistantTranscript, ToolStartTranscript
from agent_comms.turn_item import NativeItemId, NativeTurnId


def test_newest_tool_call_is_one_indexed_read_and_time_comparisons_cross_midnight(tmp_path):
    turn_turn_id = NativeTurnId('turn')
    transcript = ExternalTranscript(tmp_path, RecordedThreadIncarnation("worker", 1.0))
    assert transcript.last_tool_call() == UnrecordedEventTime()
    assert not transcript.path.exists()
    transcript.finish(turn_id=turn_turn_id, item_id=NativeItemId("first"), events=(
        ToolStartTranscript(position=0, tool_call_id=ToolCallId("first"), tool_name="bash", timestamp=86390.0),))
    transcript.finish(turn_id=turn_turn_id, item_id=NativeItemId("text"),
        events=(AssistantTranscript("newer text", position=0, timestamp=86420.0),))
    assert transcript.last_tool_call() == RecordedEventTime(86390.0)
    transcript.finish(turn_id=turn_turn_id, item_id=NativeItemId("second"), events=(
        ToolStartTranscript(position=0, tool_call_id=ToolCallId("second"), tool_name="bash", timestamp=86410.0),))
    latest = transcript.last_tool_call()
    assert latest.since(86399.0)
    assert not latest.before(86400.0)
    with transcript.snapshot() as db:
        plan = db.execute(f'EXPLAIN QUERY PLAN SELECT timestamp FROM "{ExternalEventRow.declared_name}" '
                          'WHERE thread=? AND kind=? ORDER BY seq DESC,ordinal DESC LIMIT 1',
                          (transcript.lookup, ToolStartTranscript.declared_name)).fetchall()
        assert not any("TEMP B-TREE" in row[-1] for row in plan)


