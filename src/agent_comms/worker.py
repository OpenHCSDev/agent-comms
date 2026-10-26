"""One persistent worker host per bus; native turns remain independent sessions."""

import argparse
import asyncio
import os
import signal
import sys
from contextlib import suppress

from .acp import CommsAgent
from .active_route import ActiveRoute
from .comms import Comms, wire
from .coordination_database import CoordinationStore
from .coordinator import Coordination
from .launch_root import LaunchRoot
from .root_runtime import RootRuntimeStore


async def run() -> None:
    LaunchRoot.startup(sys.argv, ActiveRoute.published_launch)
    parser = argparse.ArgumentParser(prog="agent-comms-agent", description=__doc__)
    parser.add_argument("--listener-fd", type=int, required=True)
    options = parser.parse_args()
    await run_host(wire(), options.listener_fd)


async def run_host(comms: Comms, listener_fd: int) -> None:
    """Own the root's host, not whichever unit happened to launch it first."""
    lower_priority()
    comms.owners.require_private_launch()
    comms.owners.launch_authority.check()
    host = RootRuntimeStore.at(comms.root).read().host.observed(comms.root)
    if not host.runs_here():
        raise ValueError("This process is not the root's reserved worker host")
    async with Coordination.worker(CoordinationStore.path_in(comms.root)):
        agent = CommsAgent(comms, runtime_enabled=True)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopped.set)
        async with agent.host(listener_fd):
            await stopped.wait()


# Native children inherit the host's background priority, not an interactive UI's.
OWNER_NICENESS = 10


def lower_priority() -> None:
    current = os.getpriority(os.PRIO_PROCESS, 0)
    os.setpriority(os.PRIO_PROCESS, 0, max(current, OWNER_NICENESS))


def main() -> int:
    with suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


if __name__ == "__main__":
    main()
