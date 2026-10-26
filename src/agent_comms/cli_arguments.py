"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import TYPE_CHECKING

from agent_comms.launch_root import LaunchRoot

if TYPE_CHECKING:
    from agent_comms.cli_command import CliCommand
    from agent_comms.cli_bridges.shared import BindingThread


class CliArguments(argparse.Namespace):
    command: str
    command_declaration: type[CliCommand]
    binding_thread: BindingThread

class CliArgumentParser(argparse.ArgumentParser):
    """Argument errors use Core's JSON failure boundary, including subcommands."""

    def add_root_options(self) -> None:
        self.add_argument(
            "--root", default=None,
            help="Explicit Comms root directory (otherwise uses the published active route)",
        )
        self.add_argument(LaunchRoot.argument, help=argparse.SUPPRESS)

    @classmethod
    def selected_command(cls, arguments: Sequence[str]) -> str | None:
        selection = cls(add_help=False)
        selection.add_root_options()
        selection.add_argument("command", nargs="?")
        args, _ = selection.parse_known_args(arguments)
        return args.command

    def error(self, message: str) -> None:
        raise ValueError(message)
