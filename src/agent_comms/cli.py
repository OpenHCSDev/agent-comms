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
from pathlib import Path

from .cli_commands import CliCommand
from .cli_protocol import CoreError
from .comms import client_wire
from .coordination_errors import CoordinationError
from .launch_runtime import (LaunchRecord, CommandLineLaunchRecord,
                             ExplicitLaunchRoot, CurrentLaunchRoot)


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
        help="Comms root directory (otherwise the supplied launch record's root)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for declaration in CliCommand.members_with(CliCommand):
        declaration.add_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        if LaunchRecord.argument in arguments:
            record = LaunchRecord.startup(arguments)
            args = build_parser().parse_args(arguments)
        else:
            from .active_route import CommsRoute
            args = build_parser().parse_args(arguments)
            selection = (ExplicitLaunchRoot(Path(args.root).expanduser().resolve()) if args.root
                         else CurrentLaunchRoot(CommsRoute.current().observe_root()))
            record = CommandLineLaunchRecord.entrypoint(selection)
            LaunchRecord.install(record)
        command = CliCommand.member_named(args.command).from_namespace(args)
        with client_wire(record.root_selection.path) as comms:
            return command.run(comms, sys.stdout)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, CoordinationError) as exc:
        return _fail(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
