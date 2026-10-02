"""Actual raw-pipe admission; no provider or native acceptance inferred."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import native_pi, native_prompt_send
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import AttachedChild
from agent_comms.coordinated_runtime import SelectedExecution
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
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="IGNORE")

    async def race(*args, **kwargs):
        if drift == "generation":
            with Coordination(str(root / "coordination.sqlite3")) as store:
                store.participants.advance_generation(
                    stable_thread_lookup(owner.created_at), owner.name, expected_generation=1
                )
        else:
            comms.registry.unregister(owner.name)
        return await fake(*args, **kwargs)

    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", race)
    with pytest.raises(StaleFence):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name=owner.name, native_package=tmp_path
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
async def test_actual_raw_writes_follow_committed_admission_without_global_exclusions(tmp_path, monkeypatch, direct, revoke):
    root, root_id, comms, _, people = _root(tmp_path, direct=direct)
    owner = people[2] if direct else people[1]
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    monkeypatch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
    create = AttachedChild.start
    children = []
    received = tmp_path / "received.json"
    session = root / "native-sessions" / stable_thread_lookup(owner.created_at) / "s.jsonl"

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

    def probe(fd, payload, boundary, cancelled, deadline):
        @contextmanager
        def scope():
            with boundary():
                observed.append(_held(root))
                yield
                observed.append(_held(root))

        result = write(fd, payload, scope, cancelled, deadline)
        # The same production admission cannot write twice even after all locks
        # are released. Its UNKNOWN journal reservation survives the first send.
        with pytest.raises(IdentityConflict, match="cannot be reused"), boundary():
            pytest.fail("one-use native admission reopened")
        return result

    monkeypatch.setattr(native_prompt_send, "_write_fenced", probe)
    with pytest.raises(StaleFence if revoke else native_pi.NativePiUnavailable):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name=owner.name, native_package=tmp_path
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

    from agent_comms import coordinated_runtime as runtime

    with pytest.MonkeyPatch.context() as patch:
        root, root_id, comms, _, people = _root(directory, direct=True)
        owner = people[2]
        comms.registry.declare(replace(owner, task="x" * 24000))
        patch.setattr(runtime, "_trusted_package", lambda _: None)
        patch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
        patch.setattr(native_prompt_send, "_MAX_SEND_SECONDS", 0.35)
        native_turn = TrackedTurnSession.execute

        async def bounded_turn(*args, **kwargs):
            return await native_turn(*args, **kwargs, model_wait_timeout=0.35)

        patch.setattr(TrackedTurnSession, "execute", bounded_turn)
        patch.setattr("agent_comms.backend.CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS",0.35)
        loop = asyncio.get_running_loop()
        create = AttachedChild.start
        children = []
        session = root / "native-sessions" / stable_thread_lookup(owner.created_at) / "s.jsonl"

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
            # registry custody; pipe backpressure cannot block this retirement.
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
                native_package=directory,
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
                    root=root, wire_root_id=root_id, owner_name=owner.name, native_package=directory
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
            "from test_native_send_admission import _same_loop_backpressure_case; "
            "asyncio.run(_same_loop_backpressure_case(Path(sys.argv[1]),sys.argv[2]))",
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
async def test_short_admission_contention_sends_once_after_release(
    tmp_path, monkeypatch, held_store, revoke
):
    """A busy store before admission is not an attempted native prompt."""
    import sqlite3
    import threading

    from agent_comms import coordinated_runtime as runtime
    from agent_comms.store_files import _store_lock

    root, root_id, comms, _initial, people = _root(tmp_path, direct=True)
    owner = people[2]
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    monkeypatch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
    create = AttachedChild.start
    received = tmp_path / "received.json"
    session = root / "native-sessions" / stable_thread_lookup(owner.created_at) / "s.jsonl"

    async def launch(*args, **kwargs):
        return await create(
            (sys.executable, "-c", _CHILD, str(session), str(received), "no"), **kwargs
        )

    monkeypatch.setattr(AttachedChild, "start", launch)
    write = native_prompt_send._write_fenced
    admissions = []

    def probe(fd, payload, boundary, cancelled, deadline):
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
                admissions.append(payload)
                yield

        try:
            return write(fd, payload, scope, cancelled, deadline)
        finally:
            release.set()
            competitor.join(3)
            timer.cancel()

    monkeypatch.setattr(native_prompt_send, "_write_fenced", probe)
    # The child intentionally exits after reading the prompt; this tests raw
    # admission only, not a native context or successful model response.
    with pytest.raises(StaleFence if revoke else native_pi.NativePiUnavailable):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name=owner.name, native_package=tmp_path
        ).run()
    if revoke:
        assert not received.exists() or not received.read_text()
        assert admissions == []
        return
    assert received.exists() and received.read_text()
    assert json.loads(received.read_text())["type"] == "prompt"
    assert admissions == [received.read_bytes()]


@pytest.mark.parametrize("cancel", [False, True])
def test_busy_admission_does_not_spend_write_budget_and_cancel_proves_no_bytes(cancel):
    import threading
    import time

    calls = []
    cancelled = threading.Event()
    release = threading.Event()

    @contextmanager
    def busy():
        calls.append(1)
        if not release.is_set():
            raise native_prompt_send.PromptAdmissionBusy("held by another owner")
        yield

    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    timer = threading.Timer(0.08, cancelled.set if cancel else release.set)
    try:
        timer.start()
        start = time.monotonic()
        if cancel:
            with pytest.raises(native_prompt_send.PromptSendNotWritten,match="before writing") as caught:
                native_prompt_send._write_fenced(write_fd,b"prompt\n",busy,cancelled,0.02)
            assert isinstance(caught.value.__cause__, native_prompt_send.PromptAdmissionBusy)
            with pytest.raises(BlockingIOError):
                os.read(read_fd,10)
        else:
            native_prompt_send._write_fenced(write_fd,b"prompt\n",busy,cancelled,0.02)
            assert os.read(read_fd,100)==b"prompt\n"
        assert time.monotonic() - start < 1
        assert len(calls) > 1
    finally:
        if timer:
            timer.cancel()
            timer.join()
        os.close(read_fd)
        os.close(write_fd)


def test_admitted_write_never_reenters_after_busy_post_write_failure():
    import threading
    import time

    entered = []

    @contextmanager
    def boundary():
        entered.append(1)
        yield
        raise native_prompt_send.PromptAdmissionBusy("post-write failure is not retryable")

    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    try:
        with pytest.raises(
            native_prompt_send.PromptSendUnknown,
            match="PromptAdmissionBusy: post-write failure is not retryable",
        ) as caught:
            native_prompt_send._write_fenced(
                write_fd, b"one prompt\n", boundary, threading.Event(), 1
            )
        assert entered == [1]
        assert isinstance(caught.value.__cause__, native_prompt_send.PromptAdmissionBusy)
        assert os.read(read_fd, 100) == b"one prompt\n"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_immediate_transaction_reproduces_postwrite_busy_without_replay(tmp_path):
    """The historical mechanism is reproducible, not proof of live81's cause."""
    import threading
    import time

    path = tmp_path / "coordinator.sqlite3"
    with Coordination(path, lock_timeout=0) as store:
        store.participants.register("owner", "owner", "owner", committed=True)
        reader = sqlite3.connect(path, isolation_level=None, timeout=0)
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
        entered = []

        @contextmanager
        def historical_admission():
            with store.session.transaction() as db:
                db.execute("UPDATE owner_generations SET generation=2")
                # A reader enters after BEGIN IMMEDIATE and blocks its COMMIT.
                reader.execute("BEGIN")
                reader.execute("SELECT * FROM owner_generations").fetchall()
                entered.append(1)
                yield

        try:
            with pytest.raises(
                native_prompt_send.PromptSendUnknown,
                match="OperationalError: database is locked",
            ) as caught:
                native_prompt_send._write_fenced(
                    write_fd,
                    b"one prompt\n",
                    historical_admission,
                    threading.Event(),
                    1,
                )
            assert caught.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_BUSY
            assert entered == [1]
            assert os.read(read_fd, 100) == b"one prompt\n"
            assert not store.session._connection.in_transaction
            assert (
                store.session._connection.execute(
                    "SELECT generation FROM owner_generations"
                ).fetchone()[0]
                == 1
            )
        finally:
            reader.close()
            os.close(read_fd)
            os.close(write_fd)


@pytest.mark.parametrize("direct", [False, True])
async def test_actual_native_admission_commits_before_bytes_and_releases_feedback_readers(
    tmp_path, monkeypatch, direct
):
    root, root_id, _comms, _initial, people = _root(tmp_path, direct=direct)
    owner = people[2] if direct else people[1]
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    monkeypatch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
    create = AttachedChild.start
    received = tmp_path / "received.json"
    session = root / "native-sessions" / stable_thread_lookup(owner.created_at) / "s.jsonl"
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

    def probe(fd, payload, boundary, cancelled, deadline):
        path = root / "coordination.sqlite3"
        reader = sqlite3.connect(path, isolation_level=None, timeout=0)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM native_runtime_input").fetchall()

        @contextmanager
        def checked_admission():
            try:
                with boundary():
                    assert observations == ["reader refused before bytes"]
                    # The exact epoch has committed before any raw byte. A
                    # later reader borrows it without blocking this pipe writer.
                    late = sqlite3.connect(path, isolation_level=None, timeout=0)
                    try:
                        epochs = late.execute(
                            "SELECT sent_owner_admission_generation FROM native_runtime_input"
                        ).fetchall()
                        assert len(epochs) == 1 and epochs[0][0] is not None
                    finally:
                        late.close()
                    assert _held(root) == []
                    observations.append("original admission committed and released")
                    yield
            except native_prompt_send.PromptAdmissionBusy:
                assert observations == []
                assert not received.exists()  # No raw prompt has reached the child.
                observations.append("reader refused before bytes")
                reader.execute("ROLLBACK")
                raise

        try:
            write(fd, payload, checked_admission, cancelled, deadline)
            observations.append("sent and committed once")
        finally:
            reader.close()

    monkeypatch.setattr(native_prompt_send, "_write_fenced", probe)
    # The local child supplies capability+reads prompt then exits without proof;
    # the expected downstream error is NOT a post-write admission failure.
    with pytest.raises(native_pi.NativePiUnavailable) as caught:
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name=owner.name, native_package=tmp_path
        ).run()
    assert "post-write" not in str(caught.value)
    assert observations == [
        "reader refused before bytes",
        "original admission committed and released",
        "sent and committed once",
    ]
    assert json.loads(received.read_text())["type"] == "prompt"
    assert len(children) == 1 and children[0].returncode is not None
    with Coordination(root / "coordination.sqlite3") as store:
        rows = store.session._connection.execute(
            "SELECT sent_owner_admission_generation FROM native_runtime_input"
        ).fetchall()
        assert len(rows) == 1 and rows[0][0] is not None
