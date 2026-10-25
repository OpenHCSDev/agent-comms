"""Persistent owner for a forked thread; idle turns consume no model process."""

import argparse
import asyncio
import os
import signal
import sys
from contextlib import AsyncExitStack, suppress
from functools import partial

from .acp import CommsAgent
from .active_route import ActiveRoute
from .comms import Comms, wire
from .coordination_database import CoordinationStore
from .coordinator import Coordination
from .input_key import InputKey, InputKeyText
from .launch_root import LaunchRoot
from .launch_runtime import LaunchRuntime, ThreadLaunch
from .native_arguments import GivenBinary, NativeArguments
from .turn_input_source import NoTurnAcceptance
from .turn_terminal import OwnerStop


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
        stopped = OwnerStop(comms.owners.maintenance)
        agent = CommsAgent(
            comms,
            runtime_enabled=True,
            agent_bin=GivenBinary(NativeArguments.owner_launcher(LaunchRuntime.running())),
            owner_stop=stopped,
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stopped.request)
        async with AsyncExitStack() as ownership:
            ownership.push_async_callback(lambda: agent.shutdown())
            await agent.sessions.start_owner(thread.worktree, name)
            if key := ThreadLaunch.take_startup_input():
                initial = asyncio.create_task(
                    run_startup_input(agent, name, InputKeyText.decode(key)))
                ownership.push_async_callback(asyncio.gather, initial, return_exceptions=True)
                ownership.callback(lambda: initial.cancel())
            await stopped


async def run_startup_input(agent: CommsAgent, name: str, key: InputKey) -> None:
    """Consume this launch's explicitly authorized input, never scan for retries."""
    async with agent.inputs.ensure_live_drain(name).turn_lock:
        async with agent.inputs.state(name).own_turn_task():
            def capture():
                snapshot = agent._comms.registry.snapshot()
                owner = snapshot.threads[name]
                row = agent.inputs.dispositions.lookup(key)
                if not row.queued_for(
                    owner.incarnation, snapshot.claim_generation(name), row.source_text
                ):
                    raise ValueError("Startup input no longer belongs to this owner claim_scope")
                return row
            row = await Coordination.run_worker(capture)
            await agent.inputs.emit_input_delivery_changed(name)
            await agent.turns.run_agent_turn(
                name,
                name,
                row.source_text,
                acceptance=NoTurnAcceptance(),
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
