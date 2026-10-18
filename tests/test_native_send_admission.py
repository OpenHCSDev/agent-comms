"""Actual raw-pipe claim_scope; no provider or native acceptance inferred."""

from __future__ import annotations
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
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.diagnostics import PublicationMeasurements
from agent_comms.child_process import AttachedChild
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordination_database import StoreBusy
from agent_comms.coordination_errors import IdentityConflict, StaleFence
from agent_comms.coordinator import Coordination
from agent_comms.tracked_turn import TrackedTurnSession
from test_coordinated_runtime import _fake_model, _root
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


@pytest.mark.parametrize(
    ("direct", "drift"), [(False, "generation"), (False, "registry"), (True, "registry")]
)
async def test_pre_send_drift_refuses_all_prompt_bytes(tmp_path, monkeypatch, direct, drift):
    root, root_id, comms, _, people = _root(tmp_path, direct=direct)
    owner = people[2] if direct else people[1]
    monkeypatch.setattr("agent_comms.native_pi._trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")

    async def race(*args, **kwargs):
        if drift == "generation":
            with Coordination(str(root / "coordination.sqlite3")) as store:
                store.participants.advance_generation(
                    RecordedThreadIncarnation("fixture", owner.created_at).lookup, owner.name, expected_generation=1
                )
        else:
            comms.registry.unregister(owner.name)
        return await fake(*args, **kwargs)

    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", race)
    with pytest.raises(StaleFence):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name=owner.name, native_package=pinned_native_package()
        ).run()
    assert calls == []


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


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("revoke", [False, True])
async def test_actual_raw_writes_follow_committed_claim_without_global_exclusions(tmp_path, monkeypatch, direct, revoke):
    root, root_id, comms, _, people = _root(tmp_path, direct=direct)
    owner = people[2] if direct else people[1]
    monkeypatch.setattr("agent_comms.native_pi._trusted_package", lambda _: None)
    monkeypatch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
    create = AttachedChild.start
    children = []
    received = tmp_path / "received.json"
    session = root / "native-sessions" / RecordedThreadIncarnation("fixture", owner.created_at).lookup / "s.jsonl"

    async def launch(*args, **kwargs):
        child = await create(
            (sys.executable, "-c", _CHILD, str(session), str(received), "no"), **kwargs
        )
        children.append(child)
        if revoke:
            comms.registry.unregister(owner.name)
        return child

    monkeypatch.setattr(AttachedChild, "start", launch)
    write = native_prompt_send._write_fenced
    observed = []

    def probe(fd, payload, boundary, cancelled, deadline, measurements):
        @contextmanager
        def scope():
            with boundary():
                observed.append(_held(root))
                yield
                observed.append(_held(root))

        result = write(fd, payload, scope, cancelled, deadline, measurements)
        # The same production claim_scope cannot write twice even after all locks
        # are released. Its UNKNOWN journal reservation survives the first send.
        with pytest.raises(IdentityConflict, match="cannot be reused"), boundary():
            pytest.fail("one-use native claim_scope reopened")
        return result

    monkeypatch.setattr(native_prompt_send, "_write_fenced", probe)
    with pytest.raises(StaleFence if revoke else native_pi.NativePiUnavailable):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name=owner.name, native_package=pinned_native_package()
        ).run()
    if revoke:
        assert observed == []
        assert not received.exists() or not received.read_text()
    else:
        assert json.loads(received.read_text())["type"] == "prompt"
        assert observed == [[], []]
    assert all(child.returncode is not None for child in children)
    assert _held(root) == []


async def _same_loop_backpressure_case(directory: Path, mode: str):
    """Runs in a bounded subprocess so a regression cannot hang the pytest owner."""
    import fcntl
    import signal
    import threading
    import time

    import agent_comms.coordinated_runtime as runtime

    with pytest.MonkeyPatch.context() as patch:
        root, root_id, comms, _, people = _root(directory, direct=True)
        owner = people[2]
        comms.registry.declare(replace(owner, task="x" * 24000))
        patch.setattr("agent_comms.native_pi._trusted_package", lambda _: None)
        patch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
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
                native_package=pinned_native_package(),
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
                    root=root, wire_root_id=root_id, owner_name=owner.name, native_package=pinned_native_package()
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


@pytest.mark.parametrize(
    ("held_store", "revoke"),
    [
        ("wire", False),
        ("bus.jsonl", False),
        ("registry.json", False),
        ("sql", False),
        ("wire", True),
    ],
)
async def test_short_claim_contention_sends_once_after_release(
    tmp_path, monkeypatch, held_store, revoke
):
    """A busy store before claim_scope is not an attempted native prompt."""
    import sqlite3
    import threading

    import agent_comms.coordinated_runtime as runtime
    from agent_comms.store_files import _store_lock

    root, root_id, comms, _initial, people = _root(tmp_path, direct=True)
    owner = people[2]
    monkeypatch.setattr("agent_comms.native_pi._trusted_package", lambda _: None)
    monkeypatch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
    create = AttachedChild.start
    received = tmp_path / "received.json"
    session = root / "native-sessions" / RecordedThreadIncarnation("fixture", owner.created_at).lookup / "s.jsonl"

    async def launch(*args, **kwargs):
        return await create(
            (sys.executable, "-c", _CHILD, str(session), str(received), "no"), **kwargs
        )

    monkeypatch.setattr(AttachedChild, "start", launch)
    write = native_prompt_send._write_fenced
    claims = []

    def probe(fd, payload, boundary, cancelled, deadline, measurements):
        held, release = threading.Event(), threading.Event()

        def contend():
            if held_store == "sql":
                with sqlite3.connect(root / "coordination.sqlite3") as db:
                    db.execute("BEGIN IMMEDIATE")
                    held.set()
                    assert release.wait(3)
            else:
                with _store_lock(root / held_store):
                    if revoke:
                        comms.registry.unregister(owner.name)
                    held.set()
                    assert release.wait(3)

        competitor = threading.Thread(target=contend)
        competitor.start()
        assert held.wait(3)
        timer = threading.Timer(0.1, release.set)
        timer.start()

        @contextmanager
        def scope():
            with boundary():
                claims.append(payload)
                yield

        try:
            return write(fd, payload, scope, cancelled, deadline, measurements)
        finally:
            release.set()
            competitor.join(3)
            timer.cancel()

    monkeypatch.setattr(native_prompt_send, "_write_fenced", probe)
    # The child intentionally exits after reading the prompt; this tests raw
    # claim_scope only, not a native context or successful model response.
    with pytest.raises(StaleFence if revoke else native_pi.NativePiUnavailable):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name=owner.name, native_package=pinned_native_package()
        ).run()
    if revoke:
        assert not received.exists() or not received.read_text()
        assert claims == []
        return
    assert received.exists() and received.read_text()
    assert json.loads(received.read_text())["type"] == "prompt"
    assert claims == [received.read_bytes()]


def test_transaction_excludes_late_reader_before_irreversible_write(tmp_path):
    """A transaction which admits the pipe write owns its complete commit lifetime."""
    import threading

    path = tmp_path / "coordinator.sqlite3"
    with Coordination(path, contention=StoreBusy()) as store:
        store.participants.register("owner", "owner", "owner", committed=True)
        reader = sqlite3.connect(path, isolation_level=None, timeout=0)
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
        entered = []

        @contextmanager
        def claim_scope():
            with store.session.transaction() as db:
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


@pytest.mark.parametrize("direct", [False, True])
async def test_actual_native_claim_commits_before_bytes_and_releases_feedback_readers(
    tmp_path, monkeypatch, direct
):
    root, root_id, _comms, _initial, people = _root(tmp_path, direct=direct)
    owner = people[2] if direct else people[1]
    monkeypatch.setattr("agent_comms.native_pi._trusted_package", lambda _: None)
    monkeypatch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
    create = AttachedChild.start
    received = tmp_path / "received.json"
    session = root / "native-sessions" / RecordedThreadIncarnation("fixture", owner.created_at).lookup / "s.jsonl"
    children = []

    async def launch(*_args, **kwargs):
        child = await create(
            (sys.executable, "-c", _CHILD, str(session), str(received), "no"), **kwargs
        )
        children.append(child)
        return child

    monkeypatch.setattr(AttachedChild, "start", launch)
    write = native_prompt_send._write_fenced
    observations = []

    def probe(fd, payload, boundary, cancelled, deadline, measurements):
        path = root / "coordination.sqlite3"
        reader = sqlite3.connect(path, isolation_level=None, timeout=0, check_same_thread=False)
        reader.execute("BEGIN")
        reader.execute(f"SELECT * FROM {NativeRuntimeInput.declared_name}").fetchall()

        def release_reader():
            assert not received.exists()
            reader.execute("ROLLBACK")
            observations.append("reader released before bytes")

        release = threading.Timer(0.1, release_reader)
        release.start()

        @contextmanager
        def checked_claim():
            with boundary():
                assert observations == ["reader released before bytes"]
                late = sqlite3.connect(path, isolation_level=None, timeout=0)
                try:
                    epochs = late.execute(
                        f"SELECT {NativeRuntimeInput.column_projection()['sent_owner_claimed_generation']} "
                        f"FROM {NativeRuntimeInput.declared_name}"
                    ).fetchall()
                    assert len(epochs) == 1 and epochs[0][0] is not None
                finally:
                    late.close()
                assert _held(root) == []
                observations.append("original claim_scope committed and released")
                yield

        try:
            write(fd, payload, checked_claim, cancelled, deadline, measurements)
            observations.append("sent and committed once")
        finally:
            release.join()
            reader.close()

    monkeypatch.setattr(native_prompt_send, "_write_fenced", probe)
    # The local child supplies capability+reads prompt then exits without proof;
    # the expected downstream error is NOT a post-write claim_scope failure.
    with pytest.raises(native_pi.NativePiUnavailable) as caught:
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name=owner.name, native_package=pinned_native_package()
        ).run()
    assert "post-write" not in str(caught.value)
    assert observations == [
        "reader released before bytes",
        "original claim_scope committed and released",
        "sent and committed once",
    ]
    assert json.loads(received.read_text())["type"] == "prompt"
    assert len(children) == 1 and children[0].returncode is not None
    with Coordination(root / "coordination.sqlite3") as store:
        rows = store.session._connection.execute(
            f"SELECT {NativeRuntimeInput.column_projection()['sent_owner_claimed_generation']} "
            f"FROM {NativeRuntimeInput.declared_name}"
        ).fetchall()
        assert len(rows) == 1 and rows[0][0] is not None
