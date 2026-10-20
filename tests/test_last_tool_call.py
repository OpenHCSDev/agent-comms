"""Tool-call times belong to each execution's original history, never the watcher."""


from agent_comms.external_transcript import ExternalTranscript, ExternalEventRow
from agent_comms.recorded_time import RecordedEventTime, UnrecordedEventTime
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.transcript_events import ToolStartTranscript, AssistantTranscript


def test_newest_tool_call_is_one_indexed_read_and_time_comparisons_cross_midnight(tmp_path):
    transcript = ExternalTranscript(tmp_path, RecordedThreadIncarnation("worker", 1.0))
    assert transcript.last_tool_call() == UnrecordedEventTime()
    assert not transcript.path.exists()
    transcript.finish(turn_id="turn", item_id="first", events=(
        ToolStartTranscript(position=0, tool_call_id="first", tool_name="bash", timestamp=86390.0),))
    transcript.finish(turn_id="turn", item_id="text", events=(AssistantTranscript("newer text", position=0, timestamp=86420.0),))
    assert transcript.last_tool_call() == RecordedEventTime(86390.0)
    transcript.finish(turn_id="turn", item_id="second", events=(
        ToolStartTranscript(position=0, tool_call_id="second", tool_name="bash", timestamp=86410.0),))
    latest = transcript.last_tool_call()
    assert latest.since(86399.0)
    assert not latest.before(86400.0)
    with transcript.snapshot() as db:
        plan = db.execute(f'EXPLAIN QUERY PLAN SELECT timestamp FROM "{ExternalEventRow.declared_name}" '
                          'WHERE thread=? AND kind=? ORDER BY seq DESC,ordinal DESC LIMIT 1',
                          (transcript.lookup, ToolStartTranscript.declared_name)).fetchall()
        assert not any("TEMP B-TREE" in row[-1] for row in plan)


