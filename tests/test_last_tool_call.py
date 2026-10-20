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
from agent_comms.coordination_database import CoordinationStore
from cutover.carry_tool_call_index import ToolCallIndex


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
    from agent_comms.todos import MissedFirstCallStrike, ObservedFirstCall
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
    assert context.first_calls((todo,), {'worker': context.last_activity('worker')}) == []
    comms.goals.todos.advance_objective_failures(comms.goals)
    retained = comms.goals.todos.get('P1')
    assert retained.holder == 'worker' and not retained.strikes and not retained.released
    assert isinstance(retained.order.first_call, ObservedFirstCall)


def test_tool_index_conversion_preserves_events_and_runs_once(tmp_path):
    transcript = ExternalTranscript(tmp_path, RecordedThreadIncarnation("worker", 1.0))
    transcript.finish(turn_id="turn", item_id="call", events=(
        ToolStartTranscript(position=0, tool_call_id="call", tool_name="bash", timestamp=42.0),))
    indexes = {name: sql for name, sql in ExternalEventRow.schema_objects().items()
               if sql.startswith("CREATE INDEX ") and '"seq"' in sql}
    assert len(indexes) == 1
    db = CoordinationStore.writer(transcript.path)
    try:
        before = db.execute(f'SELECT event FROM "{ExternalEventRow.declared_name}"').fetchall()
        for name in indexes:
            db.execute(f'DROP INDEX "{name}"')
        db.commit()
    finally:
        db.close()
    assert ToolCallIndex.carry(tmp_path, apply=False).changed == 1
    assert ToolCallIndex.carry(tmp_path, apply=True).changed == 1
    assert ToolCallIndex.carry(tmp_path, apply=True).changed == 0
    with transcript.snapshot() as db:
        assert [tuple(row) for row in db.execute(f'SELECT event FROM "{ExternalEventRow.declared_name}"')] == before
    assert transcript.last_tool_call() == RecordedEventTime(42.0)


def test_shared_index_carry_replaces_shifted_indexes_and_refuses_other_schema(tmp_path):
    import pytest

    from cutover.carry_external_event_indexes import ExternalEventIndexes
    from agent_comms.external_transcript import DraftRow

    assert ExternalEventIndexes.carry.__func__ is ToolCallIndex.carry.__func__
    names = tuple(ExternalEventRow.schema_objects().keys() - {ExternalEventRow.declared_name})
    shifted = sorted(names)[1:]
    for step in (ToolCallIndex, ExternalEventIndexes):
        root = tmp_path / step.__name__
        transcript = ExternalTranscript(root, RecordedThreadIncarnation("worker", 1.0))
        transcript.finish(turn_id="turn", item_id="call", events=(
            ToolStartTranscript(position=0, tool_call_id="call", tool_name="bash", timestamp=42.0),))
        with transcript.snapshot() as db:
            before = [tuple(row) for row in db.execute(
                f'SELECT event FROM "{ExternalEventRow.declared_name}"')]
        db = CoordinationStore.writer(transcript.path)
        try:
            for name in shifted:
                db.execute(f'DROP INDEX "{name}"')
            # Reproduce the real pre-P286 name collision through the Index declaration.
            db.execute(ExternalEventRow.indexes[-1].sql(ExternalEventRow, shifted[0]))
            db.commit()
        finally:
            db.close()
        assert step.carry(root, apply=False).changed == 2
        assert step.carry(root, apply=True).changed == 2
        assert step.carry(root, apply=True).changed == 0
        with transcript.snapshot() as db:
            assert [tuple(row) for row in db.execute(
                f'SELECT event FROM "{ExternalEventRow.declared_name}"')] == before
            assert tuple(db.execute("PRAGMA integrity_check").fetchone()) == ("ok",)
        db = CoordinationStore.writer(transcript.path)
        try:
            db.execute(f'DROP TABLE "{DraftRow.declared_name}"')
            db.commit()
        finally:
            db.close()
        with pytest.raises(ValueError, match="outside its allowed declared indexes"):
            step.carry(root, apply=True)
