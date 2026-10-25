"""Target-only rewrite planning shared by Core and the shipped installer."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

from agent_comms.cli_result import CliResult
from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import PathText

if TYPE_CHECKING:
    from .upgrade import Stack


@dataclass(frozen=True)
class StoreRewrite(DeclaredFamily, affix="StoreRewrite"):
    """One planned rewrite, with the original rows its rollback must retain."""

    path: Annotated[Path, PathText]

    def backup_files(self) -> tuple[Path, ...]:
        return (self.path,)


class DurableStoreRewrite(StoreRewrite):
    """At least one changing family owns original rows."""


class DerivedStoreRewrite(StoreRewrite):
    """Every changing family can regenerate its rows from retained originals."""

    def backup_files(self) -> tuple[Path, ...]:
        return ()


@dataclass(frozen=True)
class PlannedStoresResult(CliResult):
    """The installer ships this plan; outgoing reconcile replies remain unchanged."""

    rewrites: tuple[StoreRewrite, ...]

    @property
    def rewritten(self) -> tuple[Path, ...]:
        return tuple(rewrite.path for rewrite in self.rewrites)

    @property
    def backup_files(self) -> tuple[Path, ...]:
        return tuple(path for rewrite in self.rewrites for path in rewrite.backup_files())

    @classmethod
    def read(cls, stack: Stack, root: Path) -> Self:
        """Read the target's plan, not the outgoing command's older result declaration."""
        from agent_comms.cli_commands import PlannedReconcileCliCommand

        from .probes.commands import CliCommandProbe, CliOutput

        arguments = ("--root", str(root), *PlannedReconcileCliCommand.invocation())
        return stack.cli_reply(CliCommandProbe(arguments), partial(CliOutput.answer, result=cls))
