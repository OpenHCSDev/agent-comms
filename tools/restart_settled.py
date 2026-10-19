"""Print one queued restart's record once it settles, woken by its queue's own writes.

Run by the runtime whose queue holds it: ``python -I restart_settled.py ROOT ID PATIENCE``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.restart_queue import DIRECTORY, QueuedRestart, RestartQueue
from agent_comms.wire_watch import DirectoryWatchSource, WireWatch


def record(queue: RestartQueue, id: str) -> QueuedRestart:
    with queue.locked():
        settled = (queue.directory / "settled").glob(f"*/{id}.json")
        return next(record for record in (*queue.active_records(), *map(queue.read, settled))
                    if record.id == id)


async def settled(queue: RestartQueue, id: str) -> QueuedRestart:
    # Every transition rewrites or moves the record out of the active directory.
    async for _ in WireWatch.observations(DirectoryWatchSource(queue.active_directory)):
        if not (current := record(queue, id)).state.active:
            return current
    raise AssertionError("a directory watch never ends")


async def main(root: Path, id: str, patience: float) -> None:
    async with asyncio.timeout(patience):
        print(json.dumps(FieldCodec.encode(await settled(RestartQueue(root / DIRECTORY), id))))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])))
