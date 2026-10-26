"""Shared context option behavior, below the context verb."""
from __future__ import annotations
from abc import abstractmethod
import argparse
from dataclasses import dataclass
from itertools import dropwhile
from typing import ClassVar, Self
from agent_comms.cli_verbs.context import ContextCliCommand
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.turn_context import ContextManifest
from collections.abc import Iterator

class ContextOptionCliCommand(ContextCliCommand):
    @classmethod
    def add_parser[P: argparse.ArgumentParser](cls, subparsers: argparse._SubParsersAction[P]) -> None:
        """These commands are selected by the context parser's options."""

    @abstractmethod
    def apply(self, ctx: Comms) -> CliResult:
        """Inspect the context selected at the CLI boundary."""


@dataclass(frozen=True, kw_only=True)
class SelectedContextCliCommand(ContextOptionCliCommand):
    context_turn: ClassVar[bool] = True
    turn: int

    @classmethod
    def selected_arguments(cls, args: argparse.Namespace) -> Self:
        return cls(thread=args.thread, turn=args.turn)

    def manifests(self, ctx: Comms) -> Iterator[ContextManifest]:
        return dropwhile(lambda manifest: not manifest.turn.matches_generation(self.turn),
                         super().manifests(ctx))

