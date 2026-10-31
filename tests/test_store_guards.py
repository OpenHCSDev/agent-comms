"""Each store guard family, with real competing processes and exec inheritance."""
from __future__ import annotations

import select

import agent_comms
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from agent_comms.bus import Bus
from agent_comms.native_build_inputs import ConfiguredChildStdio
from agent_comms.child_process import SpawnedProcess
from agent_comms.locked_store import SQLiteFile
from agent_comms.store_files import SingleFileStore
from agent_comms.todos import TodoStore

from agent_comms_hooks.process_claim import (
    ClaimHeldError,
    ProcessSingleton,
)
from test_locked_store import _hold_store_write, assert_writer_excluded, event, process


def _competing_write(owner, path, started, entered):
    started.set()
    with owner.write_guard(path):
        entered.set()


def _read_store_guard(owner, path, done):
    with owner.read_guard(path):
        done.set()


def test_single_file_family_reads_during_writes(tmp_path):
    owners = SingleFileStore.discover_members(agent_comms, SingleFileStore)
    assert owners, "the single-file store family was not discovered"
    for owner in owners:
        path = tmp_path / owner.__name__
        ready, release, done = event(), event(), event()
        with process(_hold_store_write, owner, path, ready, release):
            try:
                assert ready.wait(5), owner
                with process(_read_store_guard, owner, path, done):
                    assert done.wait(5), f"{owner.__name__} read waited on its writer"
                started, entered = event(), event()
                with process(_competing_write, owner, path, started, entered):
                    assert started.wait(5), owner
                    assert not entered.wait(0.2), f"{owner.__name__} admitted another writer"
                    release.set()
                    assert entered.wait(5), owner
            finally:
                release.set()


def _hold_claim(path, ready, release):
    with ProcessSingleton(path).claimed():
        ready.set()
        if not release.wait(10):
            raise TimeoutError("claim holder was not released")


@contextmanager
def _exec_adopter(claim):
    with ExitStack() as owned:
        with claim.claimed() as held:
            child = owned.enter_context(SpawnedProcess.launch(
                (sys.executable, __file__, str(claim.path), str(held.fileno())),
                pass_fds=(held.fileno(),), stdio=ConfiguredChildStdio(
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE),
            ))
            assert select.select((child.process.stdout,), (), (), 10)[0], "adopter did not start"
            assert child.process.stdout.readline() == b"held\n"
        yield
        _, errors = child.process.communicate(b"release\n", timeout=10)
        assert child.process.returncode == 0, errors


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


def test_sqlite_connections_ignore_file_guards_but_maintenance_keeps_them(tmp_path):
    bus = Bus.install(tmp_path / Bus.filename)
    todos = TodoStore(tmp_path)
    with todos.writing():
        pass
    releases = []
    with ExitStack() as holders:
        try:
            for store in (bus, todos):
                ready, release = event(), event()
                releases.append(release)
                holders.enter_context(process(_hold_store_write, SQLiteFile, store.path, ready, release))
                assert ready.wait(10)
            with bus.observing() as connection:
                assert connection.execute("SELECT 1").fetchone()[0] == 1
            with todos.snapshot() as connection:
                assert connection.execute("SELECT 1").fetchone()[0] == 1
            for store in (bus, todos):
                with store.writer_connection() as connection:
                    with store.write_transaction(connection):
                        connection.execute("PRAGMA user_version = 1")
                    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        finally:
            for release in releases:
                release.set()
    for store in (bus, todos):
        with SQLiteFile.exclusive_writer(store.path) as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
            assert_writer_excluded(store.path)


if __name__ == "__main__":
    with ProcessSingleton(Path(sys.argv[1])).adopted(int(sys.argv[2])):
        print("held", flush=True)
        sys.stdin.readline()
