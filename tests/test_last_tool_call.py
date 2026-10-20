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
