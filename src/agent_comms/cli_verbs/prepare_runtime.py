"""Prepare a functional candidate through the existing runtime/preparation owners."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_command import CliCommand
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.runtime_stack import WheelRuntime
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.nominal.field_codec import Argument, FieldCodec, Normalize, PathText
from agent_comms.shell_effects import BuildStorageEffect, WriteStorageEffect
from agent_comms.storage_rules import StorageProjection
from agent_comms.tool_caller import ThreadCaller


@dataclass(frozen=True)
class PreparedRuntimeResult(CliResult):
    prefix: Annotated[Path, PathText]


@dataclass(frozen=True, kw_only=True)
class PrepareRuntimeCliCommand(CliCommand, declared_name="prepare-runtime"):
    help = ("Prepare a NEW owned functional target from an explicit interpreter, "
            "recorded dependency runtime and exact local wheels; never publish it")
    caller: Annotated[ThreadCaller, Normalize(FieldCodec.encode),
                      Argument(flags=("--from",), options={"type": ThreadCaller})]
    into: Annotated[Path, PathText, Argument(flags=("--into",))]
    interpreter: Annotated[Path, PathText, Argument(flags=("--python",))]
    source_runtime: Annotated[Path, PathText, Argument(flags=("--source-runtime",))]
    checkout: Annotated[Path, PathText, Argument(flags=("--checkout",))]
    wheels: Annotated[tuple[Annotated[Path, PathText], ...],
                     Argument(flags=("--wheels",), options={"nargs": "+"})]

    def apply(self, ctx: Comms) -> CliResult:
        thread = ctx.registry.require(self.caller.own_thread())
        prefix = self.into.expanduser().absolute()
        checkout = self.checkout.expanduser().resolve()
        projection = StorageProjection(ctx.registry.storage_rule, thread, ctx)
        projection.check((BuildStorageEffect(), WriteStorageEffect(prefix),
                          WriteStorageEffect(checkout)))
        with InstallCommands() as commands:
            target = WheelRuntime(
                checkout=checkout, commands=commands,
                runtime=LaunchRuntime.at_prefix(prefix),
                interpreter=LaunchRuntime.from_interpreter(self.interpreter.expanduser().absolute()),
                source_runtime=LaunchRuntime.at_prefix(self.source_runtime.expanduser().resolve()),
                wheels=tuple(path.expanduser().resolve(strict=True) for path in self.wheels),
            )
            target.prepare()
        return PreparedRuntimeResult(prefix)
