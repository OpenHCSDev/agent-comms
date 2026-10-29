from __future__ import annotations

from agent_comms.cli_command import PendingCommandBinding
from agent_comms.cli_support.thread_creation import ThreadCreationCliCommand, ThreadLaunchSettings
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.repo import Checkout
from agent_comms.thread_management import GivenTags
from agent_comms.thread_management import NewThreadSpec
from dataclasses import dataclass
from typing import Annotated, ClassVar


@dataclass(frozen=True, kw_only=True)
class NewThreadCliCommand(ThreadCreationCliCommand, declared_name="new"):
    help = "Create a new pi thread"
    spec_type: ClassVar[type[NewThreadSpec]] = NewThreadSpec
    tags: Annotated[GivenTags, Argument()]

    @classmethod
    def project_bindings(cls, comms: Comms, project: Checkout) -> tuple[PendingCommandBinding, ...]:
        defaults = NativeThreadDefaults()
        return (PendingCommandBinding(cls, ThreadLaunchSettings(
            project=project, model=defaults.model,
            thinking_level=defaults.thinking_level)),)
