from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_support.home_checkout import HomeCheckoutCliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import GitCommitText, PathText


@dataclass(frozen=True)
class SyncedCheckoutResult(CliResult):
    checkout: Annotated[Path, PathText]
    head: Annotated[str, GitCommitText]


@dataclass(frozen=True, kw_only=True)
class SyncCheckoutCliCommand(HomeCheckoutCliCommand, declared_name="sync-checkout"):
    help = "Fast-forward the explicit publication checkout to origin's default branch"

    def apply(self, ctx: Comms) -> SyncedCheckoutResult:
        _, checkout = self.home(ctx)
        ctx.registry.storage_rule.require_floor()
        return SyncedCheckoutResult(checkout.path, checkout.sync())
