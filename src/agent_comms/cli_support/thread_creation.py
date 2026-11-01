"""The launch settings and field-derived construction shared by new and fork."""
from __future__ import annotations

import asyncio
from abc import abstractmethod
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated

from agent_comms.cli_command import CatalogBinding, CliCommand, CommandContext
from agent_comms.cli_commands import NameArgument, ThreadCreatedResult
from agent_comms.comms import Comms
from agent_comms.config_options import ConfigOptions, ModelConfigOption, ThinkingLevelConfigOption
from agent_comms.nominal.field_codec import Argument, Catalog, Multiline
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.pi_vocabulary import ThinkingLevelField
from agent_comms.repo import Checkout, LaunchCheckout
from agent_comms.thread_management import ThreadCreationSpec
from agent_comms.thread_model import CatalogSelection, ThreadModel


@dataclass(frozen=True, kw_only=True)
class ThreadLaunchSettings(CommandContext):
    """Acquired launch settings; the creator has not supplied a child name."""
    task: Annotated[str, Multiline(), Argument(flags=("--task",))] = ""
    project: Annotated[Checkout, Argument(flags=("--project",))]
    model: Annotated[
        ThreadModel, Catalog(ModelConfigOption), Argument(flags=("--model",)),
    ] = NativeThreadDefaults.model
    thinking_level: Annotated[
        ThinkingLevelField, Catalog(ThinkingLevelConfigOption), Argument(flags=("--thinking-level",)),
    ] = NativeThreadDefaults.thinking_level

    @property
    def project_path(self) -> Path:
        return self.project.path


@dataclass(frozen=True, kw_only=True)
class ThreadCreationCliCommand(
    NameArgument, ThreadLaunchSettings, CatalogBinding, CliCommand[ThreadCreatedResult], declared_name=None,
):
    @property
    @abstractmethod
    def spec_type(self) -> type[ThreadCreationSpec]: ...

    @property
    def catalog_context(self) -> CatalogSelection:
        return self

    def specification(self) -> ThreadCreationSpec:
        return self.spec_type.from_task(**{
            **{item.name: getattr(self, item.name) for item in fields(self.spec_type)},
            'project': LaunchCheckout.checked(self.project.path),
        })

    def apply(self, ctx: Comms) -> ThreadCreatedResult:
        spec = self.specification()
        asyncio.run(ConfigOptions.configured().validate(self))
        child = ctx.threads.create(spec)
        return ThreadCreatedResult(child.name, child.process_identity)
