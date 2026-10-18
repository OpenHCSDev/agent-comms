"""Authored journal acquisition; no original retained root or runtime operation."""
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from agent_comms.goal_history import GoalHistoryError, GoalHistoryStore
from agent_comms.goals import Goal
from agent_comms.registration import Registration
from agent_comms.threads import Thread


def authored_history(root):
    root.mkdir(mode=0o700)
    registry = Registration(root / 'registry.json')
    thread = Thread('author', frozenset(), str(root))
    registry.register(thread)
    registry.register(replace(thread, goal=Goal('Authored acquisition', 'first')))
    return registry


def preserved_files(root):
    return {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_ino, p.stat().st_mtime_ns)
            for p in root.iterdir() if p.is_file()}


def test_acquire_full_rows_without_store_initialization_or_observation(tmp_path, monkeypatch):
    registry = authored_history(tmp_path / 'authored')
    before = preserved_files(registry.store.path.parent)
    def forbidden(*args, **kwargs):
        raise AssertionError('Read-only acquisition cannot initialize or observe')
    monkeypatch.setattr(GoalHistoryStore, '__init__', forbidden)
    monkeypatch.setattr(GoalHistoryStore, 'observe', forbidden)
    with registry.store.reading():
        rows = GoalHistoryStore.acquire_read_only(registry.store.path)
    assert len(rows) == 1
    assert rows[0].state == 'committed'
    assert rows[0].owner_created_at == registry.require('author').created_at
    assert rows[0].after.id == 'first'
    assert before == preserved_files(registry.store.path.parent)


def test_pending_rows_remain_pending_and_original_bytes_unchanged(tmp_path):
    registry = authored_history(tmp_path / 'authored')
    source = registry.require('author')
    store = GoalHistoryStore(registry.store.path)
    sequence = store.begin(source.created_at, source.goal, replace(source.goal, revision=1))
    before = preserved_files(registry.store.path.parent)
    with registry.store.reading():
        rows = GoalHistoryStore.acquire_read_only(registry.store.path)
    assert [(row.sequence, row.state) for row in rows] == [(1, 'committed'), (sequence, 'pending')]
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
