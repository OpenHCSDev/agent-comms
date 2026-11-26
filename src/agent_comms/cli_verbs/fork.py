from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Self

from agent_comms.cli_verbs.cli_verb import CliBus
from agent_comms.messaging.channel_targets import DeclaredTag
from agent_comms.cli_support.thread_bound import ThreadBoundCliCommand
from agent_comms.cli_verbs.new import NewThreadCliCommand
from agent_comms.nominal.field_codec import Argument, TargetBound
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.tag_choice import GivenTags
from agent_comms.registry.thread_management import ForkSpec
from agent_comms.registry.thread_model import UndeclaredModel
from agent_comms.registry.thread_settings import ThreadSettings
from agent_comms.registry.threads import ForkableThreadCommand, Thread


@dataclass(frozen=True, kw_only=True)
class ForkCliCommand(ForkableThreadCommand, ThreadBoundCliCommand, NewThreadCliCommand):
    help = "Fork a child thread from a parent's backend"
    menu_label = "Fork"
    spec_type = ForkSpec
    parent: Annotated[
        str, TargetBound(), Argument(flags=("--parent",))
    ]

    @classmethod
    def project_bindings(cls, comms: CliBus, project: Checkout) -> tuple[Self, ...]:
        return ()

    @classmethod
    def thread_binding(cls, thread: Thread) -> Self:
        settings = ThreadSettings.field_values(thread)
        settings["model"] = thread.catalog_model(UndeclaredModel())
        return cls(name='', parent=thread.name, tags=GivenTags(DeclaredTag.for_child(thread.tags)), project=thread.worktree,
                   **settings)
