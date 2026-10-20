"""Persistent owner for a forked thread; idle turns consume no model process."""

import argparse
import asyncio
import os
import signal
import sys
from contextlib import suppress
from functools import partial

from .acp import CommsAgent
from .comms import Comms, wire
from .coordinator import Coordination
from .launch_runtime import LaunchRecord


async def run() -> None:
    # Both entry points (python -m agent_comms.worker, agent-comms-agent) run here.
    lower_priority()
    record = LaunchRecord.current()
    comms = wire(record.root_selection.path)
    # Fail before attach; the check verifies the native package once.
    comms.owners.launch_authority.check()
    async with Coordination.worker(comms.root / "coordination.sqlite3"):
        await run_owner(comms)


async def run_owner(comms: Comms) -> None:
    record = LaunchRecord.current()
    name = LaunchRecord.current().require_thread().name
    thread = await Coordination.run_worker(partial(comms.registry.require, name))
    agent = CommsAgent(
        comms,
        runtime_enabled=True,
        agent_bin=record.agent_bin,
        agent_args=list(record.agent_args),
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await agent.sessions.start_owner(thread.worktree, name)
        startup = LaunchRecord.current().take_startup_input()
        async with startup.running(partial(run_startup_input, agent, name)):
            await stopped.wait()
    finally:
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
                    owner.incarnation, snapshot.claimed_generations[name], row.source_text
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
    record = LaunchRecord.startup(sys.argv)
    argparse.ArgumentParser(prog="agent-comms-agent", description=__doc__).parse_args()
    wire(record.root_selection.path).owners.require_private_launch()
    with suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
