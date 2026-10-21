"""Persistent owner for a forked thread; idle turns consume no model process."""

import argparse
import asyncio
import os
import signal
import sys
from contextlib import suppress
from functools import partial

from .acp import CommsAgent
from .active_route import ActiveRoute
from .comms import Comms, wire
from .coordination_database import CoordinationStore
from .coordinator import Coordination
from .launch_root import LaunchRoot
from .launch_runtime import LaunchRuntime, ThreadLaunch
from .native_arguments import NativeArguments


async def run() -> None:
    # The module/programmatic entrypoint selects its root exactly once.
    LaunchRoot.startup(sys.argv, ActiveRoute.published_launch)
    await run_owner(wire())


async def run_owner(comms: Comms) -> None:
    """Own the worker lifecycle on the entrypoint's already-selected wire."""
    lower_priority()
    # Fail before attach; the check verifies the native package once.
    comms.owners.launch_authority.check()
    async with Coordination.worker(CoordinationStore.path_in(comms.root)):
        name = ThreadLaunch.current().name
        thread = await Coordination.run_worker(partial(comms.registry.require, name))
        agent = CommsAgent(
            comms,
            runtime_enabled=True,
            agent_bin=NativeArguments.owner_launcher(LaunchRuntime.running()),
        )
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopped.set)
        initial: asyncio.Task | None = None
        try:
            await agent.sessions.start_owner(thread.worktree, name)
            if key := ThreadLaunch.take_startup_input():
                initial = asyncio.create_task(run_startup_input(agent, name, key))
            await stopped.wait()
        finally:
            if initial is not None:
                initial.cancel()
                await asyncio.gather(initial, return_exceptions=True)
            await agent.shutdown()


async def run_startup_input(agent: CommsAgent, name: str, key: str) -> None:
    """Consume this launch's explicitly authorized input, never scan for retries."""
    from .store_files import SharedLock, _store_lock

    async with agent.turns.turn_locks.setdefault(name, asyncio.Lock()):
        def capture():
            with _store_lock(agent._comms._wire_lock_path, mode=SharedLock()):
                snapshot = agent._comms.registry.snapshot()
                owner = snapshot.threads[name]
                row = agent.inputs.dispositions.lookup(key)
                if not row.queued_for(
                    owner.incarnation, snapshot.claim_generation(name), row.source_text
                ):
                    raise ValueError("Startup input no longer belongs to this owner claim_scope")
            return row
        row = await Coordination.run_worker(capture)
        await agent.inputs.emit_input_disposition(name, row)
        await agent.turns.run_agent_turn(
            name,
            name,
            row.source_text,
            original_keys=(key,),
            original_owner_input=True,
        )


# An owner is background work: under CPU contention it yields to interactive
# clients (Toad's UI thread) and it still uses all idle CPU. The native Pi
# processes it launches inherit this priority.
OWNER_NICENESS = 10


def lower_priority() -> None:
    """Run this owner at background priority; never raise a lower one it inherited."""
    current = os.getpriority(os.PRIO_PROCESS, 0)
    os.setpriority(os.PRIO_PROCESS, 0, max(current, OWNER_NICENESS))


def main() -> int:
    """Run the explicitly selected, already declared persistent owner.

    Catalog and tool commands belong to agent-comms, not this launcher. Parse
    before consulting a route or wire so discovery cannot start an owner.
    Registration belongs to the original thread management operation.
    """
    LaunchRoot.startup(sys.argv, ActiveRoute.published_launch)
    argparse.ArgumentParser(
        prog="agent-comms-agent", description=__doc__,
        epilog=(
            f"Select an existing owner with {ThreadLaunch.variable('name')}. "
            "Use agent-comms for commands."
        ),
    ).parse_args()
    name = ThreadLaunch.current().name

    comms = wire()
    comms.owners.require_private_launch()  # Headless execution needs a private bus root.
    thread = comms.registry.require(name)
    if thread.process_identity.alive():
        raise ValueError(f"Thread {thread.name!r} already has a live owner")
    with suppress(KeyboardInterrupt):
        asyncio.run(run_owner(comms))
    return 0


if __name__ == "__main__":
    asyncio.run(run())
