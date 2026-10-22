"""Fenced native writes use real pipes, locks and child retirement, never patched IO."""

from __future__ import annotations

import asyncio
import array
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

if sys.platform == "linux":
    import fcntl
    import termios

from agent_comms.bus import Bus
from agent_comms.child_process import AttachedChild, ProcessIdentity
from agent_comms.coordination_database import CoordinationStore, StoreBusy
from agent_comms.coordinator import Coordination
from agent_comms.declared_family import DeclaredFamily
from agent_comms.diagnostics import PublicationMeasurements
from agent_comms.locked_store import RegistryFile
from agent_comms.native_prompt_send import (
    PromptSendNotWritten,
    PromptSendUnknown,
    _write_fenced,
    send_fenced_prompt,
)
from agent_comms.store_files import store_lock_path
from conftest import installed_comms
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


_PROBE = r"""
import fcntl, json, sqlite3, sys
locks, databases = json.loads(sys.argv[1])
held = []
for path in locks:
    with open(path, 'a+b') as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held.append(path)
for path in databases:
    db = sqlite3.connect(path, timeout=0)
    try:
        db.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError as e:
        if 'locked' not in str(e):
            raise
        held.append(path)
    finally:
        db.close()
print(json.dumps(held))
"""


def _held(root):
    stores = (Bus.path_in(root), RegistryFile.path_in(root))
    databases = (Bus.path_in(root), CoordinationStore.path_in(root))
    probed = json.dumps(([str(store_lock_path(store)) for store in stores],
                         [str(database) for database in databases]))
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, probed],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return json.loads(result.stdout)


def _pipe_bytes(stdin):
    count = array.array("i", [0])
    fcntl.ioctl(stdin.get_extra_info("pipe").fileno(), termios.FIONREAD, count, True)
    return count[0]


class PromptWriteCase(DeclaredFamily, affix="WriteCase"):
    refusal = PromptSendUnknown

    def prepare(self, stdin):
        pass

    async def interrupt(self, pending, stdin, comms):
        async with asyncio.timeout(3):
            while not _pipe_bytes(stdin):
                await asyncio.sleep(0)
        self.after_partial(pending, comms)

    def after_partial(self, pending, comms):
        pass

    def check(self, stdin, entered, payload):
        assert entered == [True]
        assert 0 < _pipe_bytes(stdin) < len(payload)
        assert stdin.transport.get_write_buffer_size() == 0


class TimeoutWriteCase(PromptWriteCase):
    pass


class LifecycleWriteCase(PromptWriteCase):
    def after_partial(self, pending, comms):
        # A normal synchronous callback on the SAME loop must finish while
        # the independent writer is waiting for the child's pipe capacity.
        comms.registry.unregister("owner")
        assert not comms.registry.status("owner").active


class CancelWriteCase(PromptWriteCase):
    refusal = asyncio.CancelledError

    def after_partial(self, pending, comms):
        pending.cancel()


class RepeatCancelWriteCase(CancelWriteCase):
    def after_partial(self, pending, comms):
        super().after_partial(pending, comms)
        asyncio.get_running_loop().call_soon(pending.cancel)


class PreflightDrainWriteCase(PromptWriteCase):
    refusal = PromptSendNotWritten

    def prepare(self, stdin):
        # Real StreamWriter buffering, not a replacement drain method. The
        # raw writer must refuse before acquiring a send permission.
        stdin.write(b"earlier transport bytes" * 2000)
        assert stdin.transport.get_write_buffer_size() > 0

    async def interrupt(self, pending, stdin, comms):
        comms.registry.unregister("owner")
        assert not comms.registry.status("owner").active

    def check(self, stdin, entered, payload):
        assert entered == []
        assert stdin.transport.get_write_buffer_size() > 0


async def _same_loop_backpressure_case(directory: Path, case_name: str):
    case = PromptWriteCase.member_named(case_name)()
    comms = installed_comms(directory / "wire")
    comms.registry.declare(comms.registry.new_thread(
        "owner", frozenset(), str(directory),
        process_identity=ProcessIdentity.capture(os.getpid()),
    ))
    # The external process owns a real non-reading pipe. It impersonates no Pi
    # response or input acknowledgement; this test exercises the pipe owner.
    child = await AttachedChild.start((
        sys.executable, "-c", "import signal; print('ready', flush=True); signal.pause()",
    ))
    pending = None
    try:
        assert await child.stdout.readline() == b"ready\n"
        stdin = child.stdin
        fcntl.fcntl(stdin.get_extra_info("pipe").fileno(), fcntl.F_SETPIPE_SZ, 4096)
        case.prepare(stdin)
        payload = b"one original prompt\n" * 2000
        entered = []

        @contextmanager
        def permission():
            entered.append(True)
            yield

        started = time.monotonic()
        pending = asyncio.create_task(send_fenced_prompt(
            stdin, payload, permission,
            measurements=PublicationMeasurements(), timeout=.35,
        ))
        await case.interrupt(pending, stdin, comms)
        with pytest.raises(case.refusal):
            await asyncio.wait_for(pending, timeout=3)
        assert time.monotonic() - started < 3
        case.check(stdin, entered, payload)
        assert not any(t.name == "native-prompt-writer" for t in threading.enumerate())
        assert _held(comms.root) == []
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await child.stop()
    assert child.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)
    print("BOUNDED_UNKNOWN_REAPED", flush=True)


@pytest.mark.skipif(sys.platform != "linux", reason="deterministic Linux pipe capacity")
@pytest.mark.parametrize("case", PromptWriteCase.members_with(PromptWriteCase),
                         ids=lambda case: case.declared_name)
def test_same_loop_lifecycle_and_partial_send_cleanup_are_bounded(tmp_path, case):
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import asyncio,sys; from pathlib import Path; "
            f"from {_same_loop_backpressure_case.__module__} import {_same_loop_backpressure_case.__name__}; "
            f"asyncio.run({_same_loop_backpressure_case.__name__}(Path(sys.argv[1]),sys.argv[2]))",
            str(tmp_path), case.declared_name,
        ],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")]
        )},
        capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BOUNDED_UNKNOWN_REAPED" in result.stdout


def test_transaction_excludes_late_reader_before_irreversible_write(tmp_path):
    """A transaction which admits the pipe write owns its complete commit lifetime."""
    path = tmp_path / "coordinator.sqlite3"
    with Coordination(path, contention=StoreBusy()) as store:
        store.participants.register("owner", "owner", "owner", committed=True)
        reader = sqlite3.connect(path, isolation_level=None, timeout=0)
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
        entered = []

        @contextmanager
        def claim_scope():
            with store.session.writing() as db:
                db.execute("UPDATE owner_generations SET generation=2")
                reader.execute("BEGIN")
                try:
                    with pytest.raises(sqlite3.OperationalError) as unavailable:
                        reader.execute("SELECT * FROM owner_generations").fetchall()
                    assert unavailable.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
                finally:
                    reader.execute("ROLLBACK")
                entered.append(1)
                yield

        try:
            _write_fenced(write_fd, b"one prompt\n", claim_scope,
                          threading.Event(), 1, PublicationMeasurements())
            assert entered == [1]
            assert os.read(read_fd, 100) == b"one prompt\n"
            assert not store.session._connection.in_transaction
            assert store.session._connection.execute(
                "SELECT generation FROM owner_generations"
            ).fetchone()[0] == 2
        finally:
            reader.close()
            os.close(read_fd)
            os.close(write_fd)
