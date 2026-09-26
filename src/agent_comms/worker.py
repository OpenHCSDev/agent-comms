"""Persistent owner for a forked thread; idle turns consume no model process."""

import asyncio
import os
import signal

from .acp import CommsAgent
from .operations import wire
from .private_nk_entrypoint import private_nk_from_environment


async def run() -> None:
    private_nk = private_nk_from_environment()  # fail before wire creation/attach
    comms = wire(private_nk.validated_root) if private_nk is not None else wire()
    name = os.environ["AGENT_COMMS_THREAD"]
    thread = comms.registry.require(name)
    agent = CommsAgent(
        comms,
        runtime_enabled=True,
        private_nk_wire_root_id=private_nk.wire_root_id if private_nk else None,
        private_nk_native_package=private_nk.native_package if private_nk else None,
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
        if prompt := os.environ.get("PI_PROMPT"):
            initial = asyncio.create_task(agent.prompt(name, [{"type": "text", "text": prompt}]))
        await stopped.wait()
    finally:
        if initial is not None:
            initial.cancel()
            await asyncio.gather(initial, return_exceptions=True)
        await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
