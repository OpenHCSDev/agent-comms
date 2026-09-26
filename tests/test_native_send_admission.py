"""Actual raw-pipe admission; no provider or native acceptance inferred."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import native_pi, native_prompt_send
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.coordinated_runtime import run_one_sealed_claim
from agent_comms.coordination_store import MutationStore, StaleFence
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
            with MutationStore(str(root / "coordination.sqlite3")) as store:
                store.advance_owner_generation(
                    stable_thread_lookup(owner.created_at), owner.name, expected_generation=1
                )
        else:
            comms.registry.unregister(owner.name)
        return await fake(*args, **kwargs)

    monkeypatch.setattr("agent_comms.coordinated_runtime.run_native_pi_turn", race)
    with pytest.raises(StaleFence):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name=owner.name, native_package=tmp_path
        )
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
assert json.loads(sys.stdin.readline())['type'] == 'get_state'
print(json.dumps({'type':'response','id':'native-capability','command':'get_state',
    'success':True,'data':{'nativeInputProofCapability':'pi-native-input-v1-live-only',
                         'sessionId':'session','sessionFile':session}}), flush=True)
if stalled == 'yes':
    signal.pause()
else:
    Path(received).write_text(sys.stdin.readline())
"""


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("revoke", [False, True])
async def test_actual_raw_writes_hold_owner_exclusions(tmp_path, monkeypatch, direct, revoke):
    root, root_id, comms, _, people = _root(tmp_path, direct=direct)
    owner = people[2] if direct else people[1]
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    monkeypatch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
    create = asyncio.create_subprocess_exec
    children = []
    received = tmp_path / "received.json"
    session = root / "native-sessions" / stable_thread_lookup(owner.created_at) / "s.jsonl"

    async def launch(*args, **kwargs):
        child = await create(
            sys.executable, "-c", _CHILD, str(session), str(received), "no", **kwargs
        )
        children.append(child)
        if revoke:
            comms.registry.unregister(owner.name)
        return child

    monkeypatch.setattr(native_pi.asyncio, "create_subprocess_exec", launch)
    write = native_prompt_send._write_fenced
    observed = []

    def probe(fd, payload, boundary, cancelled, deadline):
        @contextmanager
        def scope():
            with boundary():
                observed.append(_held(root))
                yield
                observed.append(_held(root))

        return write(fd, payload, scope, cancelled, deadline)

    monkeypatch.setattr(native_prompt_send, "_write_fenced", probe)
    with pytest.raises(StaleFence if revoke else native_pi.NativePiUnavailable):
        await run_one_sealed_claim(
            root, wire_root_id=root_id, owner_name=owner.name, native_package=tmp_path
        )
    if revoke:
        assert observed == []
        assert not received.exists() or not received.read_text()
    else:
        assert json.loads(received.read_text())["type"] == "prompt"
        assert observed == [["wire", "bus.jsonl", "registry.json", "sql"]] * 2
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
        comms.register(replace(owner, task="x" * 24000))
        patch.setattr(runtime, "_trusted_package", lambda _: None)
        patch.setattr(native_pi, "_trusted_package", lambda _: Path("/bin/true"))
        patch.setattr(native_prompt_send, "_MAX_SEND_SECONDS", 0.35)
        native_turn = runtime.run_native_pi_turn

        async def bounded_turn(*args, **kwargs):
            return await native_turn(*args, **kwargs, timeout=0.35)

        patch.setattr(runtime, "run_native_pi_turn", bounded_turn)
        loop = asyncio.get_running_loop()
        create = asyncio.create_subprocess_exec
        children = []
        session = root / "native-sessions" / stable_thread_lookup(owner.created_at) / "s.jsonl"

        async def launch(*args, **kwargs):
            child = await create(
                sys.executable,
                "-c",
                _CHILD,
                str(session),
                str(directory / "unused"),
                "yes",
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

        patch.setattr(native_pi.asyncio, "create_subprocess_exec", launch)
        write = os.write
        byte_counts = []
        lifecycle_done = []
        queued = False
        task = None

        def lifecycle():
            # This is intentionally a synchronous ordinary callback on the SAME
            # loop that awaits native send. Worker deadline must free admission
            # even while this callback blocks waiting for the registry lock.
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
            run_one_sealed_claim(
                root,
                wire_root_id=root_id,
                owner_name=owner.name,
                native_package=directory,
            )
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
                await run_one_sealed_claim(
                    root, wire_root_id=root_id, owner_name=owner.name, native_package=directory
                )
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
