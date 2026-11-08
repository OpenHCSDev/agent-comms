from __future__ import annotations

from abc import abstractmethod
from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import CheckoutGrant
from agent_comms.cli_commands import CheckoutOperation
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.nominal.field_codec import Normalize
from agent_comms.nominal.field_codec import PathText
from ..projects.checkouts import RepositoryReads
from ..projects.repository_sources import RepositorySource
from ..projects.storage_rules import StorageRule
from agent_comms.registry.threads import Thread
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
                 "help": "Exact owned checkout to transfer or retire; required for transfer"}),
    ] = ()

    @abstractmethod
    def declared_destination(self, rule: StorageRule, thread: Thread) -> Path: ...

    def destination(self, ctx: Comms, thread: Thread) -> Path:
        rule = ctx.registry.storage_rule
        return rule.reused_checkout(thread, self.source(ctx, thread).github,
                                    self.declared_destination(rule, thread), RepositoryReads())

    @abstractmethod
    def grant(self, ctx: Comms, thread: Thread) -> CheckoutGrant: ...

    @abstractmethod
    def source(self, ctx: Comms, thread: Thread) -> RepositorySource: ...

    def apply(self, ctx: Comms) -> CliResult:
        thread = ctx.registry.require(self.caller.own_thread())
        return self.operation.apply(self, ctx, thread)
