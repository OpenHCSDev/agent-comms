"""Tool-call times belong to each execution's original history, never the watcher."""
import json
import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest

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


@pytest.mark.parametrize('execution_name', ('native', 'external'))
def test_execution_activity_drives_first_call_alarm_and_core_strike(
        commander, commander_script, execution_name, monkeypatch):
    from agent_comms.thread_execution import ExternalThreadExecution, NativeThreadExecution
    from agent_comms.todos import MissedFirstCallStrike
    from agent_comms.standing_rules import FirstCallMissing
    from test_commander_problem_orders import create

    comms, _ = commander
    context, _ = commander_script
    execution = {'native': NativeThreadExecution, 'external': ExternalThreadExecution}[execution_name]
    session = comms.root / 'native-sessions' / 'worker' / 'session.jsonl'
    worker = comms.registry.require('worker')
    comms.registry.register(replace(worker, execution=execution, session_file=(
        str(session) if execution is NativeThreadExecution else None)))
    worker = comms.registry.require('worker')
    todo = create(commander, 'P1', deadline=time.time() + 3600)
    assert context.last_activity('worker') == UnrecordedEventTime()
    assert MissedFirstCallStrike.observe(comms.goals, todo, 221, None)

    def append(timestamp, *, call=True):
        if execution is NativeThreadExecution:
            session.parent.mkdir(parents=True, exist_ok=True)
            message = {'role': 'assistant', 'timestamp': 999999999,
                'stopReason': 'toolUse' if call else 'stop', 'content': [
                    {'type': 'toolCall', 'id': str(timestamp), 'name': 'bash', 'arguments': {'command': 'pwd'}}
                    if call else {'type': 'text', 'text': 'newer non-tool message'}]}
            with session.open('a') as stream:
                stream.write(json.dumps({'type': 'message', 'id': str(timestamp),
                    'timestamp': datetime.fromtimestamp(timestamp, UTC).isoformat(),
                    'message': message}) + '\n')
        else:
            event = (ToolStartTranscript(position=0, tool_call_id=str(timestamp),
                     tool_name='bash', timestamp=timestamp) if call else
                     AssistantTranscript('newer non-tool message', position=0, timestamp=timestamp))
            ExternalTranscript(comms.root, worker.incarnation).finish(
                turn_id='turn', item_id=str(timestamp), events=(event,))

    append(99)
    assert context.last_activity('worker') == RecordedEventTime(99)
    assert MissedFirstCallStrike.observe(comms.goals, todo, 221, None)
    append(150)
    append(199, call=False)
    append(250)  # A later call must not erase the call that met the deadline at 220.
    append(300, call=False)
    if execution is NativeThreadExecution:
        with session.open('a') as stream:
            stream.write(json.dumps({'type': 'message', 'id': 'user',
                'timestamp': datetime.fromtimestamp(400, UTC).isoformat(),
                'message': {'role': 'user', 'content': [
                    {'type': 'toolCall', 'id': 'not-assistant', 'name': 'bash', 'arguments': {}}]}}) + '\n')
            stream.write('{"type":"message"')  # In-flight tail is not a saved tool call.
    assert context.last_activity('worker') == RecordedEventTime(250)
    source = worker.runtime.history_source(comms.root, worker.incarnation)
    assert not source.tool_call_between(151, 220)
    if execution is NativeThreadExecution:
        from agent_comms.native_entries import NativeEntry

        read = NativeEntry.read
        decoded = []

        def counted(raw):
            decoded.append(raw)
            return read(raw)

        with monkeypatch.context() as patch:
            patch.setattr(NativeEntry, 'read', counted)
            assert not source.tool_call_between(200, 220)
        # User 400, text 300, tool 250, text 199; stop even on an older non-tool row.
        assert len(decoded) == 4
    assert not MissedFirstCallStrike.observe(comms.goals, todo, 301, None)
    comms.goals.todos.advance_objective_failures(comms.goals)
    retained = comms.goals.todos.get('P1')
    assert retained.holder == 'worker' and not retained.strikes and not retained.released
    assert FirstCallMissing.violations(comms, context.commander) == ()


