"""The context CLI entrypoint and option decoder."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from agent_comms.cli_context import CurrentContextCliCommand
from agent_comms.cli_verbs.diff_context import DiffContextCliCommand
from agent_comms.cli_verbs.imported_context import ImportedContextCliCommand
from agent_comms.cli_verbs.turn_context import TurnContextCliCommand
from agent_comms.cli_verbs.turn_diff_context import TurnDiffContextCliCommand

@dataclass(frozen=True, kw_only=True)
class ContextCliCommand(CurrentContextCliCommand):
    @classmethod
    def from_arguments(cls, args: argparse.Namespace) -> ContextCliCommand:
        if args.imported:
            if "turn" in vars(args) or args.diff:
                raise ValueError("--imported cannot be combined with --turn or --diff")
            return ImportedContextCliCommand(thread=args.thread)
        if "turn" in vars(args):
            command = TurnDiffContextCliCommand if args.diff else TurnContextCliCommand
            return command(thread=args.thread, turn=args.turn)
        return (DiffContextCliCommand if args.diff else cls)(thread=args.thread)
