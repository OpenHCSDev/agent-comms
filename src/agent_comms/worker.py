"""Persistent owner for a forked thread; idle turns consume no model process."""

import asyncio
import os
import signal

from .acp import CommsAgent
from .operations import wire


async def run() -> None:
    comms = wire()
    name = os.environ["AGENT_COMMS_THREAD"]
    thread = comms.registry.require(name)
    agent = CommsAgent(comms, runtime_enabled=True)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
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
