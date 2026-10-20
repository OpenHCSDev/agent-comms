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
from collections.abc import Iterable, Iterator
from functools import partial
from pathlib import Path

from .active_route import CurrentLaunchRoot, ExplicitLaunchRoot
from .cli_bridges.shared import SendArguments
from .cli_commands import CliCommand
from .cli_protocol import CoreError
from .comms import client_wire
from .coordination_errors import CoordinationError
from .launch_runtime import LaunchRecord


def _fail(error: BaseException) -> int:
    """The declared wire family owns classification, causes and JSON encoding."""
    print(CoreError.from_exception(error).model_dump_json(), flush=True)
    return 1


class ProcessArguments:
    def __iter__(self) -> Iterator[str]:
        return iter(sys.argv[1:])


class CliArgumentParser(argparse.ArgumentParser):
    """Argument errors use Core's JSON failure boundary, including subcommands."""

    def error(self, message: str) -> None:
        raise ValueError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = CliArgumentParser(prog="agent-comms", description=__doc__)
    parser.add_argument(
        "--root",
        default=CurrentLaunchRoot.capture,
        type=lambda value: partial(ExplicitLaunchRoot, Path(value).expanduser().resolve()),
        help="Comms root directory (otherwise the supplied launch record's root)",
    )
    parser.add_argument(LaunchRecord.argument, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for declaration in CliCommand.members_with(CliCommand):
        declaration.add_parser(subparsers)
    return parser


def main(argv: Iterable[str] = ProcessArguments()) -> int:
    try:
        arguments = list(argv)
        args = build_parser().parse_args(arguments, namespace=SendArguments())
        record = LaunchRecord.client_startup(arguments, args.root())
        command = CliCommand.member_named(args.command).from_arguments(args)
        with client_wire(record.root_selection.path) as comms:
            return command.run(comms, sys.stdout)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, CoordinationError) as exc:
        return _fail(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
