"""Actual raw-pipe claim_scope; no provider or native acceptance inferred."""

from __future__ import annotations
from contextlib import closing
from agent_comms.cursor_publication import CursorDelivery
from agent_comms.native_package import VerifiedNativePackage
from agent_comms.native_package import pinned_native_package

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from agent_comms.turn_watchdog import ModelWait

import agent_comms.native_pi as native_pi
import agent_comms.native_prompt_send as native_prompt_send
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.diagnostics import PublicationMeasurements
from agent_comms.child_process import AttachedChild
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordination_database import StoreBusy
from agent_comms.coordinator import Coordination
from agent_comms.tracked_turn import TrackedTurnSession
from test_coordinated_runtime import _root
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


_PROBE = r"""
import fcntl, json, sqlite3, sys
from pathlib import Path
root = Path(sys.argv[1])
held = []
for name in ('wire', 'bus.jsonl', 'registry.json'):
    with open(root / ('.' + name + '.lock'), 'a+b') as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held.append(name)
db = sqlite3.connect(root / 'coordination.sqlite3', timeout=0)
try:
    db.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as e:
    if 'locked' not in str(e):
        raise
    held.append('sql')
finally:
    db.close()
print(json.dumps(held))
"""


def _held(root):
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(root)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


_CHILD = r"""
import json, signal, sys
from pathlib import Path
session, received, stalled = sys.argv[1:]
request = json.loads(sys.stdin.readline())
assert request['type'] == 'get_state'
print(json.dumps({'type':'response','id':request['id'],'command':'get_state',
    'success':True,'data':{'nativeInputProofCapability':'pi-native-input-v1-live-only',
                         'sessionId':'session','sessionFile':session}}), flush=True)
if stalled == 'yes':
    signal.pause()
else:
    Path(received).write_text(sys.stdin.readline())
"""


async def _same_loop_backpressure_case(directory: Path, mode: str):
    with closing(CursorDelivery()) as cursor_delivery:
        """Runs in a bounded subprocess so a regression cannot hang the pytest owner."""
        import fcntl
        import signal
        import threading
        import time


        with pytest.MonkeyPatch.context() as patch:
            root, root_id, comms, _, people = _root(directory, direct=True)
            owner = people[2]
            comms.registry.declare(replace(owner, task="x" * 24000))
            patch.setattr(native_prompt_send, "_MAX_SEND_SECONDS", 0.35)
            native_turn = TrackedTurnSession.execute

            async def bounded_turn(*args, **kwargs):
                return await native_turn(*args, **kwargs, model_wait=ModelWait(0.35))

            patch.setattr(TrackedTurnSession, "execute", bounded_turn)
            patch.setattr("agent_comms.backend.CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS",0.35)
            loop = asyncio.get_running_loop()
            create = AttachedChild.start
            children = []
            session = root / "native-sessions" / RecordedThreadIncarnation("fixture", owner.created_at).lookup / "s.jsonl"

            async def launch(*args, **kwargs):
                child = await create(
                    (sys.executable, "-c", _CHILD, str(session), str(directory / "unused"), "yes"),
                    **kwargs,
                )
                fcntl.fcntl(child.stdin.get_extra_info("pipe").fileno(), fcntl.F_SETPIPE_SZ, 4096)
                children.append(child)
                if mode == "preflight-drain":
                    original_stdin = child.stdin

                    class SuspendedDrain:
                        def __getattr__(self, name):
                            return getattr(original_stdin, name)

                        async def drain(self):
                            loop.call_soon(lifecycle)
                            await asyncio.Future()

                    child.stdin = SuspendedDrain()
                return child

            patch.setattr(AttachedChild, "start", launch)
            write = os.write
            byte_counts = []
            lifecycle_done = []
            queued = False
            task = None

            def lifecycle():
                # This is intentionally a synchronous ordinary callback on the SAME
                # loop that awaits native send. The grant has already released
                # registry ownership; pipe backpressure cannot block this retirement.
                comms.registry.unregister(owner.name)
                lifecycle_done.append(True)

            def raw_write(fd, data):
                nonlocal queued
                count = write(fd, data)
                if threading.current_thread().name == "native-prompt-writer":
                    byte_counts.append(count)
                    if not queued:
                        queued = True
                        if mode == "lifecycle":
                            loop.call_soon_threadsafe(lifecycle)
                        elif mode == "cancel":
                            loop.call_soon_threadsafe(task.cancel)
                        elif mode == "repeat-cancel":

                            def twice():
                                task.cancel()
                                loop.call_soon(task.cancel)

                            loop.call_soon_threadsafe(twice)
                return count

            patch.setattr(native_prompt_send.os, "write", raw_write)
            killpg = os.killpg

            def cancel_during_cleanup(pid, sig):
                if mode == "repeat-cancel" and sig == signal.SIGTERM:
                    loop.call_soon(task.cancel)
                return killpg(pid, sig)

            patch.setattr(native_pi.os, "killpg", cancel_during_cleanup)
            start = time.monotonic()
            task = asyncio.create_task(
                SelectedExecution(
                    root=root,
                    wire_root_id=root_id,
                    owner_name=owner.name,
                    native_package=VerifiedNativePackage.acquire(
                        pinned_native_package()
                    ),
                    cursor_delivery=cursor_delivery,
                ).run()
            )
            error = asyncio.CancelledError if "cancel" in mode else native_pi.NativePiUnavailable
            with pytest.raises(error):
                await asyncio.wait_for(task, timeout=3)
            assert time.monotonic() - start < 3
            if mode == "preflight-drain":
                assert byte_counts == []
            else:
                assert byte_counts and 0 < sum(byte_counts) < 24000
            if mode in {"lifecycle", "preflight-drain"}:
                assert lifecycle_done == [True]
            assert all(child.returncode is not None for child in children)
            for child in children:
                with pytest.raises(ProcessLookupError):
                    os.kill(child.pid, 0)
            assert not any(t.name == "native-prompt-writer" for t in threading.enumerate())
            assert _held(root) == []
            if mode not in {"lifecycle", "preflight-drain"}:
                assert (
                    await SelectedExecution(
                        root=root,
                        wire_root_id=root_id,
                        owner_name=owner.name,
                        native_package=VerifiedNativePackage.acquire(
                            pinned_native_package()
                        ),
                        cursor_delivery=cursor_delivery,
                    ).run()
                    is None
                )
                assert len(children) == 1  # reserved/uncertain input is never replayed
            print("BOUNDED_UNKNOWN_REAPED", flush=True)


@pytest.mark.skipif(sys.platform != "linux", reason="deterministic Linux pipe capacity")
@pytest.mark.parametrize(
    "mode", ["lifecycle", "timeout", "cancel", "repeat-cancel", "preflight-drain"]
)
def test_same_loop_lifecycle_and_partial_send_cleanup_are_bounded(tmp_path, mode):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; from pathlib import Path; "
            f"from {_same_loop_backpressure_case.__module__} import {_same_loop_backpressure_case.__name__}; "
            f"asyncio.run({_same_loop_backpressure_case.__name__}(Path(sys.argv[1]),sys.argv[2]))",
            str(tmp_path),
            mode,
        ],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")]
            ),
        },
        capture_output=True,
        text=True,
        timeout=8,
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
            native_prompt_send._write_fenced(
                write_fd,
                b"one prompt\n",
                claim_scope,
                threading.Event(),
                1,
                PublicationMeasurements(),
            )
            assert entered == [1]
            assert os.read(read_fd, 100) == b"one prompt\n"
            assert not store.session._connection.in_transaction
            assert (
                store.session._connection.execute(
                    "SELECT generation FROM owner_generations"
                ).fetchone()[0]
                == 2
            )
        finally:
            reader.close()
            os.close(read_fd)
            os.close(write_fd)


