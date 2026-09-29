"""Persistent owner for a forked thread; idle turns consume no model process."""

import asyncio
import os
import signal
from contextlib import suppress

from .acp import CommsAgent
from .comms import wire
from .private_nk_entrypoint import private_nk_from_environment


async def run() -> None:
    private_nk = private_nk_from_environment()  # fail before wire creation/attach
    comms = wire(private_nk.validated_root) if private_nk is not None else wire()
    if private_nk is not None:
        comms.owners.pin_private_nk_launch(
            private_nk.validated_root, private_nk.wire_root_id, private_nk.native_package
        )
    name = os.environ["AGENT_COMMS_THREAD"]
    thread = comms.registry.require(name)
    agent = CommsAgent(
        comms,
        runtime_enabled=True,
        private_nk_wire_root_id=private_nk.wire_root_id if private_nk else None,
        private_nk_native_package=private_nk.native_package if private_nk else None,
        private_selected_tool_intent=private_nk.selected_tool_intent if private_nk else None,
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stopped.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stopped.set))
    initial: asyncio.Task | None = None
    try:
        await agent.load_session(thread.worktree, name)
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
    from .store_files import _store_lock

    async with agent.turns.turn_locks.setdefault(name, asyncio.Lock()):
        with _store_lock(agent._comms._wire_lock_path):
            snapshot = agent._comms.registry.snapshot()
            owner = snapshot.threads[name]
            row = agent.inputs.dispositions.read().rows[key]
            if not row.queued_for(
                owner.incarnation, snapshot.admission_generations[name], row.source_text
            ):
                raise ValueError("Startup input no longer belongs to this owner admission")
        await agent.inputs.emit_input_disposition(name, row)
        await agent.turns.run_agent_turn(
            name,
            name,
            row.source_text,
            original_keys=(key,),
            original_owner_input=True,
        )


def main() -> int:
    """Join the configured native wire through the canonical persistent owner.

    An existing identity retains its own project. A newly registered headless
    owner uses the invoking directory; senders never select its tool worktree.
    """
    from pathlib import Path

    from .threads import Thread

    launch = private_nk_from_environment()
    if launch is None:
        raise ValueError("Headless execution requires a configured native route")
    comms = wire(launch.validated_root)
    name = os.environ.get("AGENT_COMMS_THREAD") or os.environ.get("PI_AGENT_ID") or "participant"
    if name not in comms.registry:
        tags = frozenset(filter(None, os.environ.get("PI_AGENT_TAGS", "bot").split(",")))
        comms.registry.declare(Thread(name, tags, str(Path.cwd()), task=os.environ.get("PI_TASK")))
    thread = comms.registry.require(name)
    if thread.process_identity is not None and thread.process_identity.alive():
        raise ValueError(f"Thread {thread.name!r} already has a live owner")
    os.environ["AGENT_COMMS_THREAD"] = thread.name
    with suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


if __name__ == "__main__":
    asyncio.run(run())
