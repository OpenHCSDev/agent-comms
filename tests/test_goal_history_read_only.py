"""Authored journal acquisition; no original retained root or runtime operation."""
from dataclasses import replace
import sqlite3

import pytest

from agent_comms.field_codec import GoalId
from agent_comms.goal_history import (
    CommittedEntry,
    GoalHistoryError,
    GoalHistoryStore,
    PendingEntry,
)
from agent_comms.goals import Goal
from agent_comms.registration import Registration
from agent_comms.threads import Thread


def authored_history(root):
    first_goal_id = GoalId.mint()
    root.mkdir(mode=0o700)
    registry = Registration(root / 'registry.json')
    thread = Thread('author', frozenset(), str(root))
    registry.register(thread)
    registry.register(replace(thread, goal=Goal('Authored acquisition', first_goal_id,
        published_seq=0)))
    return registry


def preserved_files(root):
    return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_ino, p.stat().st_mtime_ns)
            for p in root.iterdir() if p.is_file()}


def test_pending_rows_remain_pending_and_original_bytes_unchanged(tmp_path):
    registry = authored_history(tmp_path / 'authored')
    source = registry.require('author')
    store = GoalHistoryStore(registry.store.path)
    sequence = store.begin(source.incarnation.wire_birth, source.goal, replace(source.goal, revision=1))
    before = preserved_files(registry.store.path.parent)
    with registry.store.reading():
        rows = GoalHistoryStore.acquire_read_only(registry.store.path)
    assert [row.sequence for row in rows] == [1, sequence]
    assert rows[0].state is CommittedEntry
    assert rows[1].state is PendingEntry
    assert before == preserved_files(registry.store.path.parent)


@pytest.mark.parametrize('defect', ['missing', 'schema'])
def test_missing_or_changed_schema_refuses_without_repair(tmp_path, defect):
    registry = authored_history(tmp_path / 'authored')
    path = registry.store.path.parent / 'goal_history.sqlite3'
    if defect == 'missing':
        path.unlink()
    else:
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE unrelated (fact TEXT)')
    before = preserved_files(path.parent)
    with pytest.raises(GoalHistoryError):
        GoalHistoryStore.acquire_read_only(registry.store.path)
    assert before == preserved_files(path.parent)
