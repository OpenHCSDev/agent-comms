"""Persistent owner for a forked thread; idle turns consume no model process."""

import asyncio
import argparse
import os
import signal
from .coordinator import Coordination
from contextlib import suppress

from .acp import CommsAgent
from .comms import Comms
from .active_route import PinnedRoute
from .private_nk_entrypoint import PrivateNkLaunch
from .native_settings import NativeThreadDefaults
from .child_process import ProcessIdentity
from .launch_runtime import LaunchRuntime
from .thread_identity import ThreadIncarnation
from .store_files import _store_lock


async def run(settings: NativeThreadDefaults, incarnation: ThreadIncarnation,
              runtime: LaunchRuntime) -> None:
    # Both entry points (python -m agent_comms.worker, agent-comms-agent) run here.
    incarnation.require_recorded()
    lower_priority()
    private_nk = PrivateNkLaunch.current()
    if private_nk is None:
        raise ValueError("Headless execution requires a configured native route")
    # Verify the selected package before creating or attaching the bus.
    verified_cli = private_nk.validate()
    route = PinnedRoute(private_nk.validated_root, private_nk.wire_root_id, private_nk.native_package)
    comms = Comms(route, runtime=runtime, settings=settings)
    snapshot = await Coordination.run_worker(comms.registry.snapshot)
    if not incarnation.current(snapshot):
        raise ValueError("Owner launch does not match the current thread incarnation")
    thread = snapshot.threads[snapshot.canonical_name(incarnation.name)]
    name = thread.name
    if (thread.process_identity is not None and thread.process_identity.alive()
            and thread.process_identity != ProcessIdentity.capture(os.getpid())):
        raise ValueError(f"Thread {name!r} already has a live owner")
    agent = CommsAgent(
        comms,
        runtime_enabled=True,
        private_nk_wire_root_id=private_nk.wire_root_id,
        private_nk_native_package=private_nk.native_package,
        private_selected_tool_intent=private_nk.selected_tool_intent,
        private_nk_verified_cli=verified_cli,
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    initial: asyncio.Task | None = None
    try:
        await agent.sessions.start_owner(thread.worktree, name)
        if key := os.environ.pop("AGENT_COMMS_STARTUP_INPUT_KEY", None):
            initial = asyncio.create_task(run_startup_input(agent, name, key))
        await stopped.wait()
    finally:
        if initial is not None:
            initial.cancel()
            await asyncio.gather(initial, return_exceptions=True)
        await agent.shutdown()


async def run_startup_input(agent: CommsAgent, name: str, key: str) -> None:
    """Consume this launch's explicitly admitted input, never scan for retries."""
    async with agent.turns.turn_locks.setdefault(name, asyncio.Lock()):
        def capture():
            with _store_lock(agent._comms._wire_lock_path, shared=True):
                snapshot = agent._comms.registry.snapshot()
                owner = snapshot.threads[name]
                row = agent.inputs.dispositions.lookup(key)
                if not row.queued_for(
                    owner.incarnation, snapshot.admission_generations[name], row.source_text
                ):
                    raise ValueError("Startup input no longer belongs to this owner admission")
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
    before consulting a route or wire so discovery cannot admit an owner.
    Registration belongs to the original thread management operation.
    """
    parser = argparse.ArgumentParser(
        prog="agent-comms-agent", description=__doc__,
        epilog="Select an existing owner by its recorded incarnation. Use agent-comms for commands.",
    )
    NativeThreadDefaults.add_cli_arguments(parser, required=True, dest="native_settings")
    ThreadIncarnation.add_cli_arguments(parser, required=True, dest="thread_incarnation")
    namespace = parser.parse_args()
    runtime = LaunchRuntime.current()
    with suppress(KeyboardInterrupt):
        asyncio.run(run(namespace.native_settings, namespace.thread_incarnation, runtime))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
