"""One persistent worker host per bus; native turns remain independent sessions."""

import argparse
import asyncio
import os
import resource
import signal
import sys
from contextlib import suppress

from agent_comms.execution.acp import CommsAgent
from agent_comms.comms import Comms, wire
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.database import CoordinationStore
from agent_comms.execution.launch_root import LaunchRoot, started
from agent_comms.execution.root_runtime import RootRuntimeStore
from agent_comms.installer.active_route import ActiveRoute
from agent_comms.transport.owner_connection import socket_path


async def run() -> None:
    root = LaunchRoot.startup(sys.argv, ActiveRoute.published_launch)
    started()
    parser = argparse.ArgumentParser(prog="agent-comms-agent", description=__doc__)
    parser.add_argument("--listener-fd", type=int, required=True)
    options = parser.parse_args()
    await run_host(wire(root.path), options.listener_fd)


async def run_host(comms: Comms, listener_fd: int) -> None:
    """Own the root's host, not whichever unit happened to launch it first."""
    try:
        lower_priority()
        open_every_file_this_host_may_need()
        comms.owners.require_private_launch()
        comms.owners.launch_authority.check()
        host = RootRuntimeStore.at(comms.root).read().host.observed(comms.root)
        if not host.runs_here():
            raise ValueError("This process is not the root's reserved worker host")
        async with Coordination.worker(CoordinationStore.path_in(comms.root)):
            agent = CommsAgent(comms, auto_wake=True)
            stopped = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stopped.set)
            async with agent.host(listener_fd):
                await stopped.wait()
    finally:
        # The launcher bound this path before we ran, so a host that dies before serving owns it too.
        socket_path(comms.root, os.getpid()).unlink(missing_ok=True)


# Native children inherit the host's background priority, not an interactive UI's.
OWNER_NICENESS = 10


def lower_priority() -> None:
    current = os.getpriority(os.PRIO_PROCESS, 0)
    os.setpriority(os.PRIO_PROCESS, 0, max(current, OWNER_NICENESS))


def open_every_file_this_host_may_need() -> None:
    """Take the open-file budget this host's own kernel already allows it.

    One host answers every session on the bus, and each one holds its stores open: a
    live host sat at 748 descriptors, 157 of them on registry.sqlite3, against the 1024
    soft limit a login shell happened to hand it, while its hard limit was 524288. The
    next open() to run out of budget was an unrelated diagnostics file, so the refusal
    named that file instead of the budget, and the read that failed had nothing to do
    with the sessions holding the descriptors. The soft limit is this process's to set.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < hard:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))


def main() -> int:
    with suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


if __name__ == "__main__":
    main()
