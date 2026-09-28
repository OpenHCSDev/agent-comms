"""Real OS behavior for the shared child owner; no simulated subprocesses."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import (
    AttachedChild, BoundedRun, ChildOutcome, DetachedProcess, ExitedOutcome,
    IdentityMismatchError, Platform, ProcessIdentity, TimedOutOutcome,
)
from agent_comms.field_codec import FieldCodec

# The grandchild ignores TERM, so the owner must force retirement even after
# the group leader exits. Publishing both identities proves they really ran.
TREE = '''import subprocess,sys,os,time,json
from pathlib import Path
from agent_comms.child_process import ProcessIdentity
from agent_comms.field_codec import FieldCodec
child=subprocess.Popen([sys.executable,'-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'],stdout=subprocess.PIPE)
assert child.stdout.readline()==b'ready\\n'
Path(sys.argv[1]).write_text(json.dumps([FieldCodec.encode(ProcessIdentity.capture(p)) for p in (os.getpid(), child.pid)]))
print('tree ready',flush=True)
time.sleep(60)
'''


def identities(path: Path) -> tuple[ProcessIdentity, ...]:
    return tuple(FieldCodec.decode(ProcessIdentity, row) for row in json.loads(path.read_text()))


async def ready(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_bounded_run_retires_real_child_and_defiant_grandchild(tmp_path: Path) -> None:
    receipt = tmp_path / 'tree.json'
    result = await BoundedRun.run((sys.executable, '-c', TREE, str(receipt)), timeout=1)
    assert isinstance(result.outcome, TimedOutOutcome)
    assert result.stdout == b'tree ready\n'
    assert all(not member.alive() for member in identities(receipt))


@pytest.mark.asyncio
async def test_attached_stop_retires_real_child_and_defiant_grandchild(tmp_path: Path) -> None:
    receipt = tmp_path / 'tree.json'
    child = await AttachedChild.start((sys.executable, '-c', TREE, str(receipt)))
    try:
        await ready(receipt)
    finally:
        await child.stop()
    assert all(not member.alive() for member in identities(receipt))
    assert child.returncode is not None


@pytest.mark.asyncio
async def test_detached_mismatch_refuses_signal_without_harming_real_child() -> None:
    child = DetachedProcess.launch((sys.executable, '-c', 'import time; time.sleep(60)'))
    stale = DetachedProcess.attach(replace(child.identity, start_time=child.identity.start_time + 1))
    try:
        assert not stale.alive()
        with pytest.raises(IdentityMismatchError):
            stale.signal(signal.SIGKILL)
        with pytest.raises(IdentityMismatchError):
            await stale.stop()
        assert child.alive()
    finally:
        await child.stop()
    assert not child.alive()


@pytest.mark.asyncio
async def test_real_execution_and_external_returncode_family() -> None:
    for code in (0, 7):
        result = await BoundedRun.run((sys.executable, '-c', f'import sys; print("ok"); sys.exit({code})'), timeout=5)
        assert result.outcome == ExitedOutcome(code)
        assert result.stdout == b'ok\n'
        assert result.outcome.successful == (code == 0)


@pytest.mark.asyncio
async def test_cancellation_retains_child_cleanup(tmp_path: Path) -> None:
    receipt = tmp_path / 'tree.json'
    task = asyncio.create_task(BoundedRun.run((sys.executable, '-c', TREE, str(receipt)), timeout=10))
    await ready(receipt)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(not member.alive() for member in identities(receipt))


def test_new_outcome_is_derived_and_field_encoded() -> None:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class TestObservationOutcome(ChildOutcome):
        observation: str

        @property
        def successful(self) -> bool:
            return False

    try:
        value = TestObservationOutcome('observed')
        assert ChildOutcome.decode('test_observation') is TestObservationOutcome
        assert FieldCodec.decode(ChildOutcome, FieldCodec.encode(value)) == value
    finally:
        ChildOutcome.__registry__.pop('test_observation')
