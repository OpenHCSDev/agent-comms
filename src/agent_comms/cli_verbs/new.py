from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import NameArgument
from agent_comms.cli_commands import ThreadCreatedResult
from agent_comms.cli_commands import _tag_choice_text
from agent_comms.comms import Comms
from agent_comms.config_options import ConfigOptions
from agent_comms.config_options import ModelConfigOption
from agent_comms.config_options import ThinkingLevelConfigOption
from agent_comms.field_codec import Argument
from agent_comms.field_codec import Catalog
from agent_comms.field_codec import EditorFormat
from agent_comms.field_codec import FactoryArgumentDefault
from agent_comms.field_codec import FieldCodec
from agent_comms.field_codec import Multiline
from agent_comms.field_codec import Normalize
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.pi_vocabulary import ThinkingLevelField
from agent_comms.thread_management import InheritedTags
from agent_comms.thread_management import NewThreadSpec
from agent_comms.thread_management import TagChoice
from agent_comms.thread_model import ThreadModel
from dataclasses import dataclass
from dataclasses import fields
from typing import Annotated
from typing import ClassVar
from typing import Self
import asyncio
import os


@dataclass(frozen=True, kw_only=True)
class NewThreadCliCommand(NameArgument, CliCommand, declared_name="new"):
    help = "Create a new pi thread"
    spec_type: ClassVar[type[NewThreadSpec]] = NewThreadSpec
    task: Annotated[
        str, Multiline(), Argument(flags=("--task",))
    ] = ""
    tags: Annotated[
        TagChoice,
        EditorFormat(FieldCodec.encode),
        Normalize(_tag_choice_text),
        Argument(flags=("--tags",)),
    ] = InheritedTags()
    project: Annotated[
        str,
        Argument(
            flags=("--project",), default=FactoryArgumentDefault(os.getcwd)
        ),
    ]
    model: Annotated[
        ThreadModel, Catalog(ModelConfigOption), Argument(flags=("--model",)),
    ] = NativeThreadDefaults.model
    thinking_level: Annotated[
        ThinkingLevelField,
        Catalog(ThinkingLevelConfigOption),
        Argument(flags=("--thinking-level",)),
    ] = NativeThreadDefaults.thinking_level

    @classmethod
    def project_bindings(cls, comms: Comms, project: str) -> tuple[Self, ...]:
        defaults = NativeThreadDefaults()
        return (cls(name='', project=project, model=defaults.model,
                    thinking_level=defaults.thinking_level),)

    def apply(self, ctx: Comms) -> ThreadCreatedResult:
        asyncio.run(ConfigOptions.configured().validate(self))
        spec = self.spec_type.from_task(**{item.name: getattr(self, item.name)
                                           for item in fields(self.spec_type)})
        child = ctx.threads.create(spec)
        return ThreadCreatedResult(child.name, child.process_identity)
