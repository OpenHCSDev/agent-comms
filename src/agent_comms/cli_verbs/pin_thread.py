"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.catalog_document import CatalogDocument
from agent_comms.cli_commands import MultipleTargets
from agent_comms.cli_commands import ThreadPinnedResult
from agent_comms.cli_verbs.named import NamedCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import TargetBound
from agent_comms.thread_status import ThreadStatus
from agent_comms.threads import Thread
from dataclasses import dataclass
from typing import Annotated
from typing import Self


@dataclass(frozen=True, kw_only=True)
class PinThreadCliCommand(MultipleTargets, NamedCliCommand, declared_name="pin-thread"):
    help = "Toggle thread pin in channel"
    channel: Annotated[
        str, TargetBound(), Argument(flags=("--channel",))
    ]
    pinned: Annotated[
        bool, TargetBound(), Argument(flags=("--pinned",))
    ] = False

    @classmethod
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus, channel: str | None = None, *, catalog: CatalogDocument) -> tuple[Self, ...]:
        if channel is None:
            return ()
        with comms.bus.opened_activity() as activity:
            if not catalog.require_channel(channel, activity).matches(thread.tags):
                return ()
        return (cls(name=thread.name, channel=channel,
                    pinned=thread.name not in catalog.pinned_threads(channel)),)

    def apply(self, ctx: Comms) -> ThreadPinnedResult:
        ctx.channels.set_thread_pinned(self.channel, self.name, self.pinned)
        return ThreadPinnedResult(self.name, self.pinned)
