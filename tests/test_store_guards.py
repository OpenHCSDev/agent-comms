"""Each store guard family, with real competing processes and exec inheritance."""
from __future__ import annotations

import select
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from agent_comms.locked_store import SQLiteFile
from agent_comms.store_files import ClaimHeldError, ProcessSingleton, StoreLockContention

from test_locked_store import assert_writer_excluded, event, process


def _hold_claim(path, ready, release):
    with ProcessSingleton(path).claimed():
        ready.set()
        if not release.wait(10):
            raise TimeoutError("claim holder was not released")


def _hold_boundary(path, ready, release):
    @contextmanager
    def opening():
        with closing(sqlite3.connect(path)) as db:
            ready.set()
            if not release.wait(10):
                raise TimeoutError("attachment boundary was not released")
            yield db

    with SQLiteFile.attached(path, opening):
        pass


@contextmanager
def _exec_adopter(claim):
    with claim.claimed() as held:
        child = subprocess.Popen(
            (sys.executable, __file__, str(claim.path), str(held.fileno())),
            pass_fds=(held.fileno(),), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert select.select((child.stdout,), (), (), 10)[0], "adopter did not start"
            assert child.stdout.readline() == "held\n"
        except BaseException:
            child.kill()
            child.communicate(timeout=10)
            raise
    try:
        yield
        _, errors = child.communicate("release\n", timeout=10)
        assert child.returncode == 0, errors
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_process_singleton_refuses_competitors_and_keeps_an_exec_adoption(tmp_path):
    claim = ProcessSingleton(tmp_path / "watcher.lock")
    ready, release = event(), event()
    with process(_hold_claim, claim.path, ready, release):
        assert ready.wait(10)
        assert claim.held_elsewhere()
        with pytest.raises(ClaimHeldError), claim.claimed():
            pytest.fail("second process entered a held claim")
        release.set()
    assert not claim.held_elsewhere()
    with claim.claimed():
        with pytest.raises(ClaimHeldError), claim.claimed():
            pytest.fail("a second open description in the same process entered")
    with _exec_adopter(claim):
        # The launcher closed its description; only the exec'ed adopter holds it now.
        assert claim.held_elsewhere()
        with pytest.raises(ClaimHeldError), claim.claimed():
            pytest.fail("exec dropped the inherited claim")
    assert not claim.held_elsewhere()
    with claim.claimed() as held:
        assert held.writable()


def test_sqlite_boundaries_serialize_and_retire_with_the_real_exception(tmp_path):
    path = tmp_path / "store.sqlite3"
    opened, retired = [], []

    @contextmanager
    def opening():
        opened.append(path)
        with closing(sqlite3.connect(path)) as db:
            try:
                yield db
            finally:
                retired.append(sys.exception())
                assert_writer_excluded(path)

    ready, release = event(), event()
    with process(_hold_boundary, path, ready, release):
        assert ready.wait(10)
        with pytest.raises(BlockingIOError), SQLiteFile.attached(path, opening, StoreLockContention(0)):
            pytest.fail("attached during another process's connection boundary")
        assert opened == []
        release.set()
    failure = RuntimeError("SQL body failed")
    with pytest.raises(RuntimeError) as caught:
        with SQLiteFile.attached(path, opening) as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)
            raise failure
    assert caught.value is failure
    assert opened == [path]
    assert retired == [failure]
    with SQLiteFile.exclusive_writer(path) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


if __name__ == "__main__":
    with ProcessSingleton(Path(sys.argv[1])).adopted(int(sys.argv[2])):
        print("held", flush=True)
        sys.stdin.readline()
