"""Native startup readiness is size-aware, and cancellation reaps a starting child."""

import asyncio
import json
import os
import sys
from contextlib import aclosing

import pytest

from agent_comms.native_startup import MeasuredSessionLoad, NativeStartupPolicy, UnmeasuredSessionLoad
from pi_session_turn import one_turn_events
from agent_comms.wire_watch import WatchSource, WireWatch
from conftest import await_event_or_task

pytestmark = pytest.mark.usefixtures("native_rpc_fixture")


def test_readiness_budget_is_size_aware_bounded_and_not_a_turn_deadline():
    policy = NativeStartupPolicy()
    mib = 1024 * 1024
    for load, expected in (
        (UnmeasuredSessionLoad(), 10.0),
        (MeasuredSessionLoad(0), 10.0),
        (MeasuredSessionLoad(8 * mib), 10.0),
        (MeasuredSessionLoad(8 * mib + 1), 12.0),
        (MeasuredSessionLoad(111_533_315), 35.0),
        (MeasuredSessionLoad(112_851_639), 35.0),
        (MeasuredSessionLoad(1024 * mib), 35.0),
    ):
        assert policy.readiness_timeout(load, base_seconds=policy.readiness_seconds) == expected
    assert policy.readiness_timeout(MeasuredSessionLoad(112_851_639), base_seconds=0.05) == 26.05


@pytest.mark.asyncio
async def test_cancel_during_extended_preflight_reaps_child(
    tmp_path, monkeypatch
):
    import agent_comms.backend as backend

    policy = NativeStartupPolicy(
        readiness_seconds=0.05,
        readiness_step_bytes=1,
        readiness_step_seconds=0.5,
    )
    monkeypatch.setattr(backend, "NATIVE_STARTUP_POLICY", policy)
    monkeypatch.setattr(backend, "CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS", 0.05)
    session = tmp_path / "session.jsonl"
    # Core reads the saved header's identity before it launches the child.
    session.write_text(json.dumps({
        "type": "session", "version": 3, "id": "saved",
        "timestamp": "2026-10-14T00:00:00.000Z", "cwd": str(tmp_path),
    }) + "\n")
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
            async for event in one_turn_events(
                str(executable),
                [],
                "no prompt",
                str(tmp_path),
                session_file=str(session),
                env_extra={"AGENT_COMMS_ROOT": str(tmp_path)},
            )
        ]

    async def pid_written():
        async with aclosing(WireWatch.observations(WatchSource.modified_file(pid_file))) as changes:
            async for _ in changes:
                if pid_file.exists():
                    return

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(2):
            await await_event_or_task(pid_written(), task)
        await asyncio.sleep(0.1)
        assert not task.done()  # The size-aware budget has not expired.
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
