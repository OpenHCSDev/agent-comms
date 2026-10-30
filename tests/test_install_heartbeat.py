"""Post-restart acceptance reads ordinary service evidence, never manufactures a pass."""
from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.errors import ToolRefusedError
from agent_comms.installer.probes.services import CommanderHeartbeatProbe
from agent_comms.locked_store import DocumentChange
from agent_comms.service_passes import ServicePasses, ServicePassStore
from test_recorded_board_health import published_board


class ReadServicePassStore(ServicePassStore):
    """A real file read signals the fixture; no sleep or private-state polling."""

    def __init__(self, path: Path, reads: asyncio.Queue[ServicePasses]) -> None:
        super().__init__(path)
        object.__setattr__(self, "reads", reads)
        object.__setattr__(self, "loop", asyncio.get_running_loop())

    def read(self) -> ServicePasses:
        passes = super().read()
        self.loop.call_soon_threadsafe(self.reads.put_nowait, passes)
        return passes


@pytest.mark.parametrize("recorded", (False, True), ids=("missing", "stale"))
async def test_heartbeat_wait_rejects_old_evidence_and_wakes_on_commit(
    published_board: Comms, recorded: bool,
) -> None:
    comms = published_board
    owner = comms.registry.require("lead").incarnation.require_recorded()
    services = ServicePassStore.at(comms.root)
    since = time.time()
    services.update(lambda passes: DocumentChange.to(replace(
        passes, commander_completed={owner: since - 3600} if recorded else {},
    )))
    reads: asyncio.Queue[ServicePasses] = asyncio.Queue()
    observed = ReadServicePassStore(services.path, reads)
    pending = asyncio.create_task(observed.wait_completed((owner,), since, "CommanderHeartbeat"))
    try:
        async with asyncio.timeout(10):
            first = await reads.get()
            assert not first.completed_since(owner, since)
            assert not pending.done()
            await asyncio.to_thread(services.heartbeat_passed, (owner,), (owner,))
            await pending
        assert services.read().completed_since(owner, since)
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending


async def test_heartbeat_wait_refuses_recorded_failure_even_with_fresh_completion(
    published_board: Comms,
) -> None:
    comms = published_board
    owner = comms.registry.require("lead").incarnation.require_recorded()
    services = ServicePassStore.at(comms.root)
    since = time.time()
    services.heartbeat_passed((owner,), (owner,))
    services.failed("CommanderHeartbeat", ValueError("actual source cannot be read"))
    before = services.path.read_bytes()
    with pytest.raises(ToolRefusedError, match="actual source cannot be read"):
        await services.wait_completed((owner,), since, "CommanderHeartbeat")
    assert services.path.read_bytes() == before


def test_target_heartbeat_probe_accepts_original_current_completion(published_board: Comms) -> None:
    comms = published_board
    owner = comms.registry.require("lead").incarnation.require_recorded()
    services = ServicePassStore.at(comms.root)
    completed = services.read().commander_completed[owner]
    before = services.path.read_bytes()
    CommanderHeartbeatProbe(comms.root, completed).run()
    assert services.path.read_bytes() == before


def test_target_heartbeat_probe_reports_unconfigured_root(private_comms: Comms, capsys) -> None:
    services = ServicePassStore.at(private_comms.root)
    present = services.path.exists()
    CommanderHeartbeatProbe(private_comms.root, time.time()).run()
    assert "no registered commander; not qualified" in capsys.readouterr().out
    assert services.path.exists() == present
