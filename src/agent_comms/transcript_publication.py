"""What a session's work looks like on the bus, whichever way its transcript arrives.

A member supplies only `transcript()`: a bridge beside the session reads its file, and a watch socket
reads the lines the session's far end sends. Turn state, tool activity, batching and the wait on a
busy store are declared here once, so no route to the bus can show less than another. A remote
session once read as connected in Toad with nothing in its chat, because its socket had been written
beside the bridge instead of under this parent.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from functools import partial
from typing import ClassVar

from agent_comms.agent_runtime import PublishesTranscript
from agent_comms.cli_bridges.runtime_updates import RuntimeUpdate, ThreadOutput
from agent_comms.comms import Comms
from agent_comms.coordination.errors import CoordinationReadUnavailable
from agent_comms.execution.child_process import Platform


class WaitsOnBusyStore:
    """The one wait on a busy store: no write began and no snapshot was acquired, so the attempt
    is whole and runs again. A write that began keeps its own error."""

    @staticmethod
    def log(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    async def once_available[Result](
        self, waiting: str, attempt: Callable[[], Awaitable[Result]],
    ) -> Result:
        while True:
            try:
                return await attempt()
            except CoordinationReadUnavailable as error:
                self.log(f'{waiting} waits for the store: {error}')
                await asyncio.sleep(Platform.current().store_lock_interval)


class PublishesToBus(WaitsOnBusyStore, PublishesTranscript):
    """A session's turns and items published on its own thread as they arrive."""

    output_type: ClassVar[type[ThreadOutput]] = ThreadOutput
    comms: Comms
    transcript_thread: str

    async def confirm_delivery(self, update: RuntimeUpdate) -> RuntimeUpdate:
        """A transcript this route did not deliver into confirms nothing."""
        return update

    async def publish_transcript(self) -> None:
        comms = self.comms
        output = self.output_type.for_comms(
            await asyncio.to_thread(comms.transcripts.external, self.transcript_thread), comms)
        # This subscription can assemble only items whose start it receives.
        # A prefix from the previous subscription cannot stand for that stream.
        await self.once_available('Transcript start', partial(
            comms.bus.when_open, output.transcript.end_turn))
        # Each CLI reports its own initial state. Disk writes never stop the
        # event reader from receiving a newer whole draft of the same item.
        queue: asyncio.Queue[RuntimeUpdate] = asyncio.Queue()

        async def read() -> None:
            try:
                async for update in self.transcript():
                    queue.put_nowait(await update.observed(self))
            finally:
                queue.shutdown()

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(read())
            while True:
                try:
                    pending = [await queue.get()]
                except asyncio.QueueShutDown:
                    break
                while not queue.empty():
                    pending.append(queue.get_nowait())
                await self.once_available('Transcript update', partial(
                    comms.bus.when_open, partial(output.publish_batch, pending)))
