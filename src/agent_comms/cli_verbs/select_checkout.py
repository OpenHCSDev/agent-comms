"""Select an existing owned checkout without moving the conversation's project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import CheckoutResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument, FieldCodec, Normalize, PathText
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.transport.tool_caller import ThreadCaller


@dataclass(frozen=True, kw_only=True)
class SelectCheckoutCliCommand(CliCommand, declared_name="select-checkout"):
    help = "Choose this repository's owned editable checkout, keeping other choices"
    caller: Annotated[
        ThreadCaller,
        Normalize(FieldCodec.encode),
        Argument(flags=("--from",), options={"type": ThreadCaller}),
    ]
    repository: Annotated[RepositoryName, Argument(flags=("repository",))]
    path: Annotated[Path, PathText, Argument(flags=("path",))]

    def apply(self, ctx: Comms) -> CheckoutResult:
        return CheckoutResult(
            ctx.registry.select_checkout(self.caller.identity(ctx), self.repository, self.path)
        )
