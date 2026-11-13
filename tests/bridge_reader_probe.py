"""A real bridge reader held until its parent closes stdin or kills it."""
import asyncio
import sys
from contextlib import ExitStack
from pathlib import Path

from agent_comms.cli_bridges.codex import CodexBridge, CodexOptions
from agent_comms.cli_bridges.shared import Binding, Inbox, StoredBinding
from agent_comms.comms import wire
from agent_comms.registry.activity import ActivityState


async def run(root: Path, socket: str):
    comms = wire(root)
    binding = Binding(root=str(root), owner=comms.registry.require('cli').incarnation,
                      thread='held-session', start=Binding.bus_head(str(root)))
    with Binding.open(root / 'probe') as db:
        options = CodexOptions(Inbox(), StoredBinding(), root / 'probe', root, socket)
        bridge = CodexBridge(options, binding.address(), db)
        await options.operation.prepare(bridge)
        with ExitStack() as lifetime:
            async with asyncio.TaskGroup() as tasks:
                await options.operation.start(bridge, tasks, lifetime)
                comms.agents.set_activity('cli', ActivityState.WORKING, 'held reader')
                print('ready', flush=True)
                await asyncio.to_thread(sys.stdin.buffer.read)
                for task in tuple(asyncio.all_tasks()):
                    if task is not asyncio.current_task():
                        task.cancel()


if __name__ == '__main__':
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
