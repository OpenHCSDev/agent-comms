from __future__ import annotations

from agent_comms.cli_command import PendingCommandBinding
from agent_comms.cli_support.thread_creation import ThreadCreationCliCommand, ThreadLaunchSettings
from agent_comms.cli_support.thread_bound import ThreadBoundCliCommand
from agent_comms.nominal.field_codec import Argument
from agent_comms.nominal.field_codec import TargetBound
from agent_comms.thread_execution import NativeOwnerCommand
from agent_comms.thread_management import ForkSpec
from agent_comms.thread_management import InheritedTags, TagChoice
from agent_comms.threads import Thread
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class ForkContext(ThreadLaunchSettings):
    parent: Annotated[str, TargetBound(), Argument(flags=("--parent",))]


@dataclass(frozen=True, kw_only=True)
class ForkCliCommand(NativeOwnerCommand, ThreadBoundCliCommand, ForkContext, ThreadCreationCliCommand):
    help = "Fork a child pi thread"
    spec_type = ForkSpec

    tags: Annotated[TagChoice, Argument()] = InheritedTags()

    @classmethod
    def thread_binding(cls, thread: Thread) -> PendingCommandBinding:
        return PendingCommandBinding(ForkContext(
            parent=thread.name, project=thread.worktree,
            model=thread.model, thinking_level=thread.thinking_level))
