"""Real ACP owner/native fixture; explicit auto-wake policy holds operator dispatch."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from agent_comms.active_route import ActiveRoute
from agent_comms.comms import wire
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordinator import Coordination
from agent_comms.launch_root import LaunchRoot
from agent_comms.native_arguments import GivenArguments, GivenBinary
from delivery_owner_fixture import canonical_agent, reply_turn_finished
from native_backend_fixture import NativeBackendFixture


async def wait_for(path: Path):
    async with asyncio.timeout(30):
        while not path.exists():
            await asyncio.sleep(0.01)


async def main(base: Path) -> None:
    LaunchRoot.startup(sys.argv, ActiveRoute.published_launch)
    comms = wire()
    await wait_for(base / "registered")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    async with Coordination.worker(CoordinationStore.path_in(comms.root)):
        agent = canonical_agent(
            comms, auto_wake=False, runtime_enabled=True,
            agent_bin=GivenBinary("pi"), agent_args=GivenArguments(NativeBackendFixture.native_arguments()),
        )
        try:
            await agent.sessions.start_owner(str(base / "project"), "beta")
            print(json.dumps({"event": "ready", "pid": os.getpid()}), flush=True)
            await wait_for(base / "dispatch")
            agent.inputs.auto_wake = True
            count = await agent._drain_private_nk(
                "beta", comms.owners.require_private_launch().wire_root_id,
            )
            await reply_turn_finished(agent, "beta")
            print(json.dumps({"event": "terminal", "pid": os.getpid(), "drained": count}), flush=True)
            await stopped.wait()
        finally:
            await agent.shutdown()
            assert not agent.turns.persistent_backends
            print(json.dumps({"event": "retired", "pid": os.getpid()}), flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
