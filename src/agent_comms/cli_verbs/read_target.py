from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Annotated, Self

from agent_comms.cli_verbs.cli_command import MultipleTargets, TargetActionCommand
from agent_comms.cli_verbs.cli_commands import TargetReadResult
from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import SurroundingChannel
from agent_comms.nominal.field_codec import Argument, TargetBound
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.registry_document import RegistrySnapshot, ResolvedChannel
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import Thread


@dataclass(frozen=True, kw_only=True)
class ReadTargetCliCommand(MultipleTargets, TargetActionCommand[TargetReadResult],
                           declared_name="read-target"):
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
