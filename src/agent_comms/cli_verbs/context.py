"""The context verb owns its option selection."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from typing import ClassVar, Self
from agent_comms.cli_context import CurrentContextCliCommand, ContextSelection

@dataclass(frozen=True, kw_only=True)
class ContextCliCommand(CurrentContextCliCommand):
    context_imported: ClassVar[bool] = False
    context_diff: ClassVar[bool] = False
    context_turn: ClassVar[bool] = False

    @classmethod
    def selection_key(cls) -> ContextSelection:
        return ContextSelection(cls.context_imported, cls.context_diff, cls.context_turn)

    @classmethod
    def selected_arguments(cls, args: argparse.Namespace) -> Self:
        return cls(thread=args.thread)

    @classmethod
    def from_arguments(cls, args: argparse.Namespace) -> Self:
        turn = "turn" in vars(args)
        if args.imported and (turn or args.diff):
            raise ValueError("--imported cannot be combined with --turn or --diff")
        selected = cls.member_selected(ContextSelection(args.imported, args.diff, turn))
        return selected.selected_arguments(args)
