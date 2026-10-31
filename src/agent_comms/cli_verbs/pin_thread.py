from __future__ import annotations

from agent_comms.channel_targets import SurroundingChannel

from agent_comms.catalog_document import CatalogAccess
from agent_comms.cli_command import ChannelBound, MultipleTargets
from agent_comms.cli_commands import ThreadPinnedResult
from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.nominal.field_codec import TargetBound
from agent_comms.thread_status import ThreadStatus
from agent_comms.threads import Thread
from dataclasses import dataclass
from typing import Annotated
from typing import Self


@dataclass(frozen=True, kw_only=True)
class PinThreadCliCommand(MultipleTargets, NamedCliCommand, declared_name="pin-thread"):
    help = "Toggle thread pin in channel"
    channel: Annotated[
        str, ChannelBound(), Argument(flags=("--channel",))
    ]
    pinned: Annotated[
        bool, TargetBound(), Argument(flags=("--pinned",))
    ] = False

    @classmethod
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus, channel: SurroundingChannel, *, catalog: CatalogAccess) -> tuple[Self, ...]:
        with comms.bus.opened_activity() as activity:
            return tuple(
                cls(name=thread.name, channel=name,
                    pinned=thread.name not in catalog.pinned_threads(name))
                for name in channel.names_for_binding()
                if catalog.require_channel(name, activity).matches(thread.tags)
            )

    def apply(self, ctx: Comms) -> ThreadPinnedResult:
        ctx.channels.set_thread_pinned(self.channel, self.name, self.pinned)
        return ThreadPinnedResult(self.name, self.pinned)
