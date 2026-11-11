from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated, ClassVar, Self

from agent_comms.cli_verbs.cli_command import CreatesThread, TargetActionCommand
from agent_comms.cli_verbs.cli_commands import NameArgument, ThreadCreatedResult, _tag_choice_text
from agent_comms.comms import Comms
from agent_comms.config_options import ConfigOptions, ModelConfigOption, ThinkingLevelConfigOption
from agent_comms.execution.pi_vocabulary import ThinkingLevelField
from agent_comms.nominal.field_codec import (
    Argument,
    Catalog,
    EditorFormat,
    FieldCodec,
    Multiline,
    Normalize,
)
from agent_comms.projects.checkouts import Checkout, LaunchCheckout
from agent_comms.registry.native_settings import NativeThreadDefaults
from agent_comms.registry.thread_management import InheritedTags, NewThreadSpec, TagChoice
from agent_comms.registry.thread_model import CatalogSelection, ThreadModel


@dataclass(frozen=True, kw_only=True)
class NewThreadCliCommand(
    CreatesThread, NameArgument, CatalogSelection, TargetActionCommand[ThreadCreatedResult],
    declared_name="new",
):
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
        Checkout,
        Argument(flags=("--project",)),
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
    def project_bindings(cls, comms: Comms, project: Checkout) -> tuple[Self, ...]:
        defaults = NativeThreadDefaults()
        return (cls(name='', project=project, model=defaults.model,
                    thinking_level=defaults.thinking_level),)

    @property
    def project_path(self) -> Path:
        return self.project.path

    def apply(self, ctx: Comms) -> ThreadCreatedResult:
        spec = self.spec_type.from_task(**{
            **{item.name: getattr(self, item.name) for item in fields(self.spec_type)},
            'project': LaunchCheckout.checked(self.project.path),
        })
        asyncio.run(ConfigOptions.configured().validate(self))
        child = ctx.threads.create(spec)
        return ThreadCreatedResult(child.name, child.process_identity)
