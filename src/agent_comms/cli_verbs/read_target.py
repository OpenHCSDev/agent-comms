"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.catalog_document import CatalogDocument
from agent_comms.channel_targets import ResolvedChannel
from agent_comms.cli_command import CliCommand
from agent_comms.cli_command import MultipleTargets
from agent_comms.cli_commands import TargetReadResult
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import TargetBound
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.thread_status import ThreadStatus
from agent_comms.threads import Thread
from dataclasses import dataclass
from dataclasses import replace
from typing import Annotated
from typing import Self
import dataclasses
import os


@dataclass(frozen=True, kw_only=True)
class ReadTargetCliCommand(MultipleTargets, CliCommand, declared_name="read-target"):
    help = "Mark view read"
    target: Annotated[
        str, TargetBound(), Argument(flags=("--target",))
    ]
    worktree: Annotated[
        str, TargetBound(), Argument(flags=("--worktree",))
    ] = dataclasses.field(default_factory=os.getcwd)

    @classmethod
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus, channel: str | None = None, *, catalog: CatalogDocument | None = None) -> tuple[Self, ...]:
        return (cls(target=thread.name),)

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, snapshot: RegistrySnapshot, catalog: CatalogDocument | None):
        return (cls(target=resolved.channel.name),)

    def for_editor(self, comms: Comms, target: str, project: str) -> Self:
        return replace(self, worktree=project)

    def apply(self, ctx: Comms) -> TargetReadResult:
        ctx.views.mark_user_view_read(self.target, worktree=self.worktree)
        return TargetReadResult(self.target)
