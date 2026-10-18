import asyncio
from pathlib import Path

from agent_comms.native_entries import NativeInputEvidenceRead
from agent_comms.worker import run

SESSION = Path(__import__('os').environ['MEMORY_FORK_SESSION'])

async def workload():
    while True:
        with NativeInputEvidenceRead.open(SESSION) as reader:
            reader.observe()
        await asyncio.sleep(5)

async def main():
    task = asyncio.create_task(workload())
    try:
        await run()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

asyncio.run(main())
