from __future__ import annotations

from agent_comms.repo import Checkout

from agent_comms.channel_targets import SurroundingChannel

from agent_comms.catalog_document import CatalogAccess
from agent_comms.registry_document import ResolvedChannel
from agent_comms.cli_command import CliCommand
from agent_comms.cli_command import MultipleTargets
from agent_comms.cli_commands import TargetReadResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.nominal.field_codec import TargetBound
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.thread_status import ThreadStatus
from agent_comms.threads import Thread
from dataclasses import dataclass
from dataclasses import replace
from typing import Annotated
from typing import Self


@dataclass(frozen=True, kw_only=True)
class ReadTargetCliCommand(MultipleTargets, CliCommand, declared_name="read-target"):
    help = "Mark view read"
    target: Annotated[
        str, TargetBound(), Argument(flags=("--target",))
    ]
    worktree: Annotated[
        Checkout, TargetBound(), Argument(flags=("--worktree",))
    ]

    @classmethod
    def thread_bindings(cls, comms: Comms, thread: Thread, status: ThreadStatus, channel: SurroundingChannel, *, catalog: CatalogAccess) -> tuple[Self, ...]:
        return (cls(target=thread.name, worktree=thread.worktree),)

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return (cls(target=resolved.channel.name, worktree=project),)

    def for_editor(self, comms: Comms, target: str, project: Checkout) -> Self:
        return replace(self, worktree=project)

    def apply(self, ctx: Comms) -> TargetReadResult:
        ctx.views.mark_user_view_read(self.target, worktree=self.worktree)
        return TargetReadResult(self.target)
