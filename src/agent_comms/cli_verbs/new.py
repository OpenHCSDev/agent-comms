from __future__ import annotations

from agent_comms.cli_support.thread_creation import TagArgument, ThreadCreationCliCommand
from agent_comms.comms import Comms
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.repo import Checkout
from agent_comms.thread_management import GivenTags
from agent_comms.thread_management import NewThreadSpec
from dataclasses import dataclass
from typing import ClassVar
from typing import Self


@dataclass(frozen=True, kw_only=True)
class NewThreadCliCommand(ThreadCreationCliCommand, declared_name="new"):
    help = "Create a new pi thread"
    spec_type: ClassVar[type[NewThreadSpec]] = NewThreadSpec
    tags: TagArgument[GivenTags]

    @classmethod
    def project_bindings(cls, comms: Comms, project: Checkout) -> tuple[Self, ...]:
        defaults = NativeThreadDefaults()
        return (cls(name='', project=project, model=defaults.model,
                    thinking_level=defaults.thinking_level),)
