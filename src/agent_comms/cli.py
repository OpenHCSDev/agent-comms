"""OpenHCS agent communications — JSON CLI.

Commands print one JSON result, except watch, which streams messages as JSON
lines. Errors are one JSON line carrying a declared type and error text.
They exit 0 on success or 1 on failure. This is the adapter surface for the pi extension and other
process-based clients: they shell out and parse JSON, they do not own
orchestration semantics.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .cli_bridges.shared import SendArguments
from .cli_commands import CliCommand
from .cli_protocol import CoreError
from .comms import client_wire
from .coordination_errors import CoordinationError
from .launch_root import LaunchRoot


def _fail(error: BaseException) -> int:
    """The declared wire family owns classification, causes and JSON encoding."""
    print(CoreError.from_exception(error).model_dump_json(), flush=True)
    return 1


class CliArgumentParser(argparse.ArgumentParser):
    """Argument errors use Core's JSON failure boundary, including subcommands."""

    def error(self, message: str) -> None:
        raise ValueError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = CliArgumentParser(prog="agent-comms", description=__doc__)
    parser.add_argument(
        "--root",
        default=None,
        help="Explicit Comms root directory (otherwise uses the published active route)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for declaration in CliCommand.members_with(CliCommand):
        declaration.add_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = list(argv) if isinstance(argv, Sequence) else sys.argv[1:]
        LaunchRoot.startup(arguments)
        args = build_parser().parse_args(arguments, namespace=SendArguments())
        root = LaunchRoot.from_path(args.root) if args.root else LaunchRoot.current()
        with root.selected():
            command = CliCommand.member_named(args.command).from_arguments(args)
            with client_wire(root.path) as comms:
                return command.run(comms, sys.stdout)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, CoordinationError) as exc:
        return _fail(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
