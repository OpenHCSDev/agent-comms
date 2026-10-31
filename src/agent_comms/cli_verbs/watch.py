from __future__ import annotations

from agent_comms.active_route import CommsRoute
from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import ThreadArgument
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.agent_inbox import TakenInputStart
from agent_comms.message_reference import InboxStart
from dataclasses import dataclass
from typing import Annotated
from typing import TextIO
import json
import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from agent_comms.cli_protocol import ArrivedMessage, EarlierItems
from agent_comms.cli_result import CliResult
from agent_comms.maintenance_barrier import MaintenanceHeldError
from agent_comms.read_basis import LineOutput
from agent_comms.messages import Message


@dataclass(frozen=True)
class WatchStream(CliResult):
    """Unread and arriving messages, written as JSON lines for as long as the watch lasts."""

    comms: Comms
    watch: WatchCliCommand

    def messages(self) -> AsyncIterator[Message]:
        return self.comms.watch(self.watch.thread, after=self.watch.after)

    def write(self, output: TextIO) -> int:
        emit = LineOutput(output).write
        async def stream() -> None:
            async with aclosing(self.messages()) as messages:
                async for message in messages:
                    emit(ArrivedMessage(message=message).model_dump_json())

        async def watch() -> None:
            with Comms.client(self.comms.route) as comms:
                thread = comms.registry.require(self.watch.thread)
                source = thread.runtime.history_source(comms.root, thread.incarnation)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(stream())
                tasks.create_task(source.watch_requests(
                    lambda row: emit(EarlierItems.from_row(row).model_dump_json())))

        try:
            asyncio.run(watch())
        except* MaintenanceHeldError as held:  # the bridge reads it as its one held line
            raise held.exceptions[0] from None
        return self.exit_code()


@dataclass(frozen=True, kw_only=True)
class WatchCliCommand(ThreadArgument, CliCommand[WatchStream], declared_name="watch"):
    help = "Stream untaken and arriving messages as JSON lines; streaming records nothing"
    after: Annotated[
        InboxStart,
        Argument(
            flags=("--after",),
            options={
                "type": json.loads,
                "help": "Exclusive original message reference already attempted by this receiver (JSON)",
            },
        ),
    ] = TakenInputStart()

    def apply(self, ctx: Comms) -> WatchStream:
        return WatchStream(ctx, self)

    def execute(self, route: CommsRoute, output: TextIO) -> int:
        with Comms.client(route) as comms:
            stream = self.apply(comms)
        return stream.write(output)
