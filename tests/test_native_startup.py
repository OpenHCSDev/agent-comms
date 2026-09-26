"""Startup slots gate only cold preparation and release on process death/cancellation."""

import asyncio
import os
import subprocess
import sys

import pytest

from agent_comms.native_startup import NativeStartupAdmission, NativeStartupPolicy


def test_readiness_budget_is_size_aware_bounded_and_not_a_turn_deadline():
    policy = NativeStartupPolicy()
    mib = 1024 * 1024
    assert policy.slots == 4
    assert policy.readiness_timeout(None) == 5.0
    assert policy.readiness_timeout(0) == 5.0
    assert policy.readiness_timeout(8 * mib) == 5.0
    assert policy.readiness_timeout(8 * mib + 1) == 7.0
    assert policy.readiness_timeout(111_533_315) == 31.0
    assert policy.readiness_timeout(112_851_639) == 31.0
    assert policy.readiness_timeout(1024 * mib) == 35.0
    assert policy.readiness_timeout(112_851_639, base_seconds=0.05) == 26.05


@pytest.mark.asyncio
async def test_startup_slot_cross_process_and_crash_release(tmp_path):
    policy = NativeStartupPolicy(slots=1)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; from pathlib import Path; "
            "from agent_comms.native_startup import NativeStartupAdmission,NativeStartupPolicy; "
            "a=NativeStartupAdmission(Path(sys.argv[1]),NativeStartupPolicy(slots=1)); "
            "asyncio.run(a.acquire()); print('locked',flush=True); sys.stdin.read()",
            str(tmp_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    lease = NativeStartupAdmission(tmp_path, policy)
    try:
        assert await asyncio.to_thread(child.stdout.readline) == b"locked\n"
        waiting = asyncio.create_task(lease.acquire())
        await asyncio.sleep(0.075)
        assert not waiting.done()
        child.kill()
        await asyncio.to_thread(child.wait)
        await asyncio.wait_for(waiting, 2)
        assert lease.fd is not None
    finally:
        lease.release()
        if child.poll() is None:
            child.kill()
        child.communicate()


@pytest.mark.asyncio
async def test_queued_cancel_never_acquires_or_spawns(tmp_path):
    first = NativeStartupAdmission(tmp_path, NativeStartupPolicy(slots=1))
    queued = NativeStartupAdmission(tmp_path, NativeStartupPolicy(slots=1))
    await first.acquire()
    waiting = asyncio.create_task(queued.acquire())
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert queued.fd is None
    first.release()
    await asyncio.wait_for(queued.acquire(), 1)
    queued.release()


@pytest.mark.asyncio
async def test_cancel_during_extended_preflight_reaps_child_and_releases_slot(
    tmp_path, monkeypatch
):
    from agent_comms import backend

    policy = NativeStartupPolicy(
        slots=1,
        readiness_seconds=0.05,
        readiness_step_bytes=1,
        readiness_step_seconds=0.5,
    )
    monkeypatch.setattr(backend, "NATIVE_STARTUP_POLICY", policy)
    monkeypatch.setattr(backend, "CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS", 0.05)
    session = tmp_path / "session.jsonl"
    session.write_bytes(b"xx")
    pid_file = tmp_path / "child.pid"
    executable = tmp_path / "pi-stub"
    executable.write_text(
        f"#!{sys.executable}\n"
        f"import os, sys, time\n"
        f"assert 'get_state' in sys.stdin.readline()\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        f"time.sleep(20)\n"
    )
    executable.chmod(0o755)

    async def consume():
        return [
            event
            async for event in backend.stream_agent_events(
                str(executable),
                [],
                "no prompt",
                str(tmp_path),
                session_file=str(session),
                env_extra={"AGENT_COMMS_ROOT": str(tmp_path)},
            )
        ]

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(2):
            while not pid_file.exists():
                await asyncio.sleep(0.005)
        await asyncio.sleep(0.1)
        assert not task.done()  # The size-aware budget has not expired.
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        leases = [NativeStartupAdmission(tmp_path) for _ in range(4)]
        try:
            for lease in leases:
                await asyncio.wait_for(lease.acquire(), 0.5)
        finally:
            for lease in leases:
                lease.release()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_backend_cancel_while_waiting_starts_no_child(tmp_path, monkeypatch):
    from agent_comms import backend

    leases = [NativeStartupAdmission(tmp_path) for _ in range(4)]
    for lease in leases:
        await lease.acquire()
    executable = tmp_path / "pi-never-spawn"
    executable.write_text("not executed")
    finish = asyncio.Event()
    spawned = []

    async def spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("Queued cancellation must not spawn a child")

    monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", spawn)

    async def consume():
        return [
            event
            async for event in backend.stream_agent_events(
                str(executable),
                [],
                "never sent",
                str(tmp_path),
                env_extra={"AGENT_COMMS_ROOT": str(tmp_path)},
                finish_event=finish,
            )
        ]

    task = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0.075)
        assert not spawned and not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not spawned
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for lease in leases:
            lease.release()
