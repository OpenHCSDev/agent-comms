import asyncio

from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.watch_passes import ConnectorWatch, Watch
from agent_comms.execution.worker.__main__ import team_heartbeat_host
from agent_comms.team.team_heartbeat import TeamHeartbeat


async def test_persistent_root_worker_owns_the_only_team_heartbeat(monkeypatch, comms) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def watch_worker(watch, opened_wire, passes) -> None:
        assert isinstance(watch, TeamHeartbeat)
        assert passes.path.is_relative_to(comms.root)
        with opened_wire() as opened:
            assert opened.root == comms.root
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(Coordination, "watch_worker", staticmethod(watch_worker))

    async with team_heartbeat_host(comms):
        await asyncio.wait_for(started.wait(), 1)
        assert TeamHeartbeat not in Watch.members_with(ConnectorWatch)

    assert stopped.is_set()
