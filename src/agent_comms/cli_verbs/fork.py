from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Self

from agent_comms.messaging.channel_targets import DeclaredTag
from agent_comms.cli_support.thread_bound import ThreadBoundCliCommand
from agent_comms.cli_verbs.new import NewThreadCliCommand
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, TargetBound
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_management import ForkSpec, GivenTags
from agent_comms.registry.threads import NativeOwnerCommand, Thread


@dataclass(frozen=True, kw_only=True)
class ForkCliCommand(NativeOwnerCommand, ThreadBoundCliCommand, NewThreadCliCommand):
    help = "Fork a child pi thread"
    spec_type = ForkSpec
    parent: Annotated[
        str, TargetBound(), Argument(flags=("--parent",))
    ]

    @classmethod
    def project_bindings(cls, comms: Comms, project: Checkout) -> tuple[Self, ...]:
        return ()

    @classmethod
    def thread_binding(cls, thread: Thread) -> Self:
        return cls(name='', parent=thread.name, tags=GivenTags(DeclaredTag.for_child(thread.tags)), project=thread.worktree,
                   model=thread.model, thinking_level=thread.thinking_level)
