from __future__ import annotations

from agent_comms.channel_targets import SurroundingChannel

from agent_comms.catalog_document import CatalogAccess
from agent_comms.cli_commands import ThreadTagsResult
from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.launch_runtime import TagListText
from agent_comms.thread_status import ThreadStatus
from agent_comms.threads import Thread
from dataclasses import dataclass
from typing import Annotated
from typing import Self


@dataclass(frozen=True, kw_only=True)
class ThreadTagsCliCommand(NamedCliCommand, declared_name="thread-tags"):
    help = "Edit thread tags"
    tags: Annotated[
        frozenset[str],
        TagListText,
        Argument(
            flags=("--tags",), options={"help": "Complete tags, separated by commas"}
        ),
    ]

    @classmethod
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus, channel: SurroundingChannel, *, catalog: CatalogAccess) -> tuple[Self, ...]:
        return (cls(name=thread.name, tags=thread.tags),)

    def apply(self, ctx: Comms) -> ThreadTagsResult:
        thread = ctx.channels.replace_tags(self.name, self.tags)
        return ThreadTagsResult(thread.name, thread.tags)
