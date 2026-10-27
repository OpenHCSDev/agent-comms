"""Exact, real process death releases maintenance without a timeout or PID table fake."""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import signal
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.maintenance_barrier import (
    DeadHolderReleasedPhase,
    DrainingPhase,
    InstallingPhase,
    MaintenanceBarrier,
    MaintenanceClaimHolder,
    MaintenanceHolder,
    MaintenanceHeldError,
    MaintenanceOperator,
    MaintenancePhase,
    MaintenanceState,
    NoMaintenanceHolder,
    PausedPhase,
)
from agent_comms.store_files import (
    ExclusiveLock,
    SharedLock,
    StoreGuard,
    _async_store_lock,
    _store_lock,
)


def _hold(root: Path, phase: type[MaintenancePhase], ready) -> None:
    barrier = MaintenanceBarrier(root / "registry.json")
    held = barrier.begin(MaintenanceOperator.current())
    for successor in (PausedPhase, InstallingPhase):
        if held.phase is phase:
            break
        held = barrier.advance(held, successor)
    ready.send(json.dumps(FieldCodec.encode(held)))
    ready.close()
    signal.pause()


@contextmanager
def real_holder(root: Path, phase: type[MaintenancePhase] = DrainingPhase):
    receive, send = mp.Pipe(duplex=False)
    child = mp.Process(target=_hold, args=(root, phase, send), daemon=True)
    child.start()
    send.close()
    try:
        assert receive.poll(10), "Real holder did not publish its hold"
        held = FieldCodec.decode(MaintenanceState, FieldCodec.parse(receive.recv()))
        assert held.operator.pid == child.pid and held.operator.alive()
        yield child, held
    finally:
        if child.is_alive():
            child.kill()
        child.join(5)
        receive.close()
        assert not child.is_alive()


def test_operator_uses_process_identity_codec() -> None:
    operator = MaintenanceOperator.current()
    assert operator.pid == os.getpid() and operator.start_time > 0
    assert FieldCodec.encode(operator) == {"pid": operator.pid, "start_time": operator.start_time}
    assert FieldCodec.decode(MaintenanceOperator, FieldCodec.encode(operator)) == operator
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(MaintenanceOperator, "upgrade-bus")


@pytest.mark.parametrize("phase", [DrainingPhase, PausedPhase, InstallingPhase])
@pytest.mark.parametrize("file,mode", [("wire", SharedLock()), ("registry.json", ExclusiveLock())])
def test_dead_holder_refuses_detecting_input_then_opens(tmp_path, phase, file, mode):
    root = tmp_path / "bus"
    barrier = MaintenanceBarrier(root / "registry.json")
    with real_holder(root, phase) as (child, held):
        child.kill()
        child.join(5)
        with _store_lock(root / file, mode, StoreGuard.contention):
            with pytest.raises(
                MaintenanceHeldError,
            ):
                barrier.assert_open_unlocked()
        released = barrier.read()
        assert released.phase is DeadHolderReleasedPhase
        assert released.operator == held.operator
        assert released.generation == held.generation + 1
        with barrier.allow_input():
            pass
        assert barrier.read() == released
        # A released record is READY behavior, not a special-case phase table.
        replacement = barrier.begin(MaintenanceOperator.current())
        assert replacement.phase is DrainingPhase
        barrier.clear(replacement)


async def test_async_recovery_borrows_registry_without_blocking_event_loop(tmp_path: Path):
    root = tmp_path / "bus"
    barrier = MaintenanceBarrier(root / "registry.json")
    with real_holder(root) as (child, _held):
        child.kill()
        child.join(5)

        async def incoming():
            async with barrier.ingress_async():
                pytest.fail("The detecting input must be refused")

        async with _async_store_lock(root / 'registry.json', ExclusiveLock(), StoreGuard.contention):
            attempt = asyncio.create_task(incoming())
            await asyncio.sleep(0.025)
            assert not attempt.done()
        with pytest.raises(MaintenanceHeldError):
            await asyncio.wait_for(attempt, 5)
        async with barrier.ingress_async() as status:
            status.require_open()


async def test_concurrent_request_claims_do_not_change_each_others_permissions(tmp_path):
    from agent_comms.coordinator import Coordination

    barrier = MaintenanceBarrier(tmp_path / "registry.json")
    held = barrier.begin(MaintenanceOperator.current())
    selected, refused = asyncio.Event(), asyncio.Event()
    environment = dict(os.environ)
    original = MaintenanceHolder.current()

    def admitted():
        with barrier.allow_input():
            return "allowed"

    async def holder():
        with MaintenanceClaimHolder(held.nonce).selected():
            selected.set()
            await refused.wait()
            assert await Coordination.run_worker(admitted) == "allowed"

    async def unrelated():
        await selected.wait()
        try:
            with NoMaintenanceHolder().selected():
                with pytest.raises(MaintenanceHeldError):
                    await Coordination.run_worker(admitted)
        finally:
            refused.set()

    try:
        await asyncio.wait_for(asyncio.gather(holder(), unrelated()), 5)
        assert MaintenanceHolder.current() == original
        assert dict(os.environ) == environment
    finally:
        barrier.clear(held)


