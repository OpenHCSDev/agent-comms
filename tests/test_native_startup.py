"""Startup slots gate only cold preparation and release on process death/cancellation."""

import asyncio
import subprocess
import sys

import pytest

from agent_comms.native_startup import NativeStartupAdmission, NativeStartupPolicy


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
