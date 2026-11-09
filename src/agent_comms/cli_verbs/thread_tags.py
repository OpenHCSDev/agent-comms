from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Self

from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.cli_verbs.cli_command import TargetActionCommand
from agent_comms.cli_verbs.cli_commands import ThreadTagsResult, _tag_text, _tags
from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import SurroundingChannel
from agent_comms.nominal.field_codec import Argument, EditorFormat, Normalize
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import Thread


@dataclass(frozen=True, kw_only=True)
class ThreadTagsCliCommand(NamedCliCommand, TargetActionCommand[ThreadTagsResult],
                           declared_name="thread-tags"):
    help = "Edit thread tags"
    tags: Annotated[
        frozenset[str],
        Normalize(_tags),
        EditorFormat(_tag_text),
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
