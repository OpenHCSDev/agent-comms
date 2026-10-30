from __future__ import annotations

from abc import abstractmethod
from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import CheckoutGrant
from agent_comms.cli_commands import CheckoutOperation
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import FieldCodec
from agent_comms.field_codec import Normalize
from agent_comms.field_codec import PathText
from agent_comms.repo import Repo
from agent_comms.storage_rules import StorageRule
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class PreparedCheckoutCliCommand(CliCommand):
    caller: Annotated[
        ThreadCaller,
        Normalize(FieldCodec.encode),
        Argument(
            flags=("--from",),
            options={"type": ThreadCaller, "help": "Calling thread"},
        ),
    ]
    operation: Annotated[type[CheckoutOperation], Argument(flags=("operation",))]
    source_checkout: Annotated[
        tuple[Annotated[Path, PathText], ...],
        Argument(flags=("--source",), options={"nargs": 1,
                 "help": "Existing isolated checkout to transfer; required for transfer"}),
    ] = ()

    @abstractmethod
    def destination(self, rule: StorageRule, thread: Thread) -> Path: ...

    @abstractmethod
    def grant(self, ctx: Comms, thread: Thread) -> CheckoutGrant: ...

    def source(self, ctx: Comms, grant: CheckoutGrant) -> Repo:
        return grant.source()

    def apply(self, ctx: Comms) -> CliResult:
        thread = ctx.registry.require(self.caller.own_thread())
        destination = self.destination(ctx.registry.storage_rule, thread)
        return self.operation.apply(self, ctx, thread, destination)
