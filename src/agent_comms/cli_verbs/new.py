from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated, ClassVar, Self

from agent_comms.cli_verbs.cli_verb import CliBus
from agent_comms.cli_verbs.cli_command import CreatesThread, TargetActionCommand
from agent_comms.cli_verbs.cli_commands import NameArgument, ThreadCreatedResult, _tag_choice_text
from agent_comms.cli_verbs.model_context import ModelContextArguments
from agent_comms.config_options import ConfigOptions
from agent_comms.nominal.field_codec import (
    Argument,
    EditorFormat,
    FieldCodec,
    Multiline,
    Normalize,
)
from agent_comms.projects.checkouts import Checkout, LaunchCheckout
from agent_comms.registry.native_settings import NativeThreadDefaults
from agent_comms.registry.tag_choice import InheritedTags, TagChoice
from agent_comms.registry.thread_management import NewThreadSpec
from agent_comms.registry.thread_model import CatalogSelection
from agent_comms.registry.thread_settings import ThreadSettings


@dataclass(frozen=True, kw_only=True)
class NewThreadCliCommand(
    ModelContextArguments, ThreadSettings, CreatesThread, NameArgument, CatalogSelection,
    TargetActionCommand[ThreadCreatedResult],
    declared_name="new",
):
    help = "Create a new pi thread"
    menu_label = "New thread"
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
    model: FieldCodec.field_annotation(ThreadSettings, "model") = NativeThreadDefaults.model
    thinking_level: FieldCodec.field_annotation(ThreadSettings, "thinking_level") = NativeThreadDefaults.thinking_level

    @classmethod
    def project_bindings(cls, comms: CliBus, project: Checkout) -> tuple[Self, ...]:
        defaults = NativeThreadDefaults()
        return (cls(name='', project=project, **NativeThreadDefaults.field_values(defaults)),)

    @property
    def project_path(self) -> Path:
        return self.project.path

    def apply(self, ctx: CliBus) -> ThreadCreatedResult:
        spec = self.spec_type.from_task(**{
            **{item.name: getattr(self, item.name) for item in fields(self.spec_type)},
            'project': LaunchCheckout.checked(self.project.path),
        })
        options = ConfigOptions.for_route(ctx.route)
        asyncio.run(options.validate(self))
        self.apply_model_context(options.launch.configuration, self.model)
        child = ctx.threads.create(spec)
        return ThreadCreatedResult(child.name, child.process_identity)
