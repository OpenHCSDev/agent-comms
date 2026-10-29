from __future__ import annotations

from agent_comms.cli_support.thread_creation import TagArgument, ThreadCreationCliCommand
from agent_comms.cli_support.thread_bound import ThreadBoundCliCommand
from agent_comms.field_codec import Argument
from agent_comms.field_codec import TargetBound
from agent_comms.thread_execution import NativeOwnerCommand
from agent_comms.thread_management import ForkSpec
from agent_comms.thread_management import InheritedTags, TagChoice
from agent_comms.threads import Thread
from dataclasses import dataclass
from typing import Annotated
from typing import Self


@dataclass(frozen=True, kw_only=True)
class ForkCliCommand(NativeOwnerCommand, ThreadBoundCliCommand, ThreadCreationCliCommand):
    help = "Fork a child pi thread"
    spec_type = ForkSpec
    parent: Annotated[
        str, TargetBound(), Argument(flags=("--parent",))
    ]

    tags: TagArgument[TagChoice] = InheritedTags()

    @classmethod
    def thread_binding(cls, thread: Thread) -> Self:
        return cls(name='', parent=thread.name, project=thread.worktree,
                   model=thread.model, thinking_level=thread.thinking_level)
