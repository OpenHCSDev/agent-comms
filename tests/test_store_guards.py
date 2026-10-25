"""Each store guard family, held by a real second process."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from agent_comms.locked_store import SQLiteFile
from agent_comms.store_files import ClaimHeldError, ProcessSingleton, StoreLockContention

from test_locked_store import event, process


def _hold_claim(path, ready, release):
    with ProcessSingleton(path).claimed():
        ready.set()
        if not release.wait(10):
            raise TimeoutError("claim holder was not released")


def _hold_boundary(path, ready, release):
    with SQLiteFile.exclusive_writer(path):
        ready.set()
        if not release.wait(10):
            raise TimeoutError("boundary holder was not released")


def test_process_singleton_refuses_a_second_holder_until_the_first_exits(tmp_path):
    claim = ProcessSingleton(tmp_path / "watcher.lock")
    ready, release = event(), event()
    with process(_hold_claim, claim.path, ready, release):
        assert ready.wait(10)
        assert claim.held_elsewhere()
        with pytest.raises(ClaimHeldError), claim.claimed():
            pytest.fail("second holder entered a held claim")
        release.set()
    assert not claim.held_elsewhere()
    with claim.claimed() as held:
        assert held.writable()


def test_sqlite_attachment_waits_for_another_process_exclusive_writer(tmp_path):
    path = tmp_path / "store.sqlite3"
    opened = []

    def opening():
        opened.append(path)
        return closing(sqlite3.connect(path))

    ready, release = event(), event()
    with process(_hold_boundary, path, ready, release):
        assert ready.wait(10)
        with pytest.raises(BlockingIOError), SQLiteFile.attached(path, opening, StoreLockContention(0)):
            pytest.fail("attached while another process held an exclusive writer")
        assert opened == []
        release.set()
    with SQLiteFile.attached(path, opening) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)
    assert opened == [path]
