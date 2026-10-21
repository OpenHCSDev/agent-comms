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

from agent_comms.active_route import resolve_comms_route
from agent_comms.cli_bridges.shared import SendArguments
from agent_comms.cli_commands import CliCommand
from agent_comms.cli_protocol import CoreError
from agent_comms.coordination_errors import CoordinationError
from agent_comms.launch_root import LaunchRoot


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
    # Parse the launch option without deciding its root; LaunchRoot owns that choice.
    parser.add_argument(LaunchRoot.argument, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for declaration in CliCommand.members_with(CliCommand):
        declaration.add_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = list(argv) if isinstance(argv, Sequence) else sys.argv[1:]
        args, unknown = build_parser().parse_known_args(arguments, namespace=SendArguments())
        if unknown and (len(unknown) != 2 or unknown[0] != LaunchRoot.argument):
            raise ValueError(f"unrecognized arguments: {' '.join(unknown)}")
        declaration = CliCommand.member_named(args.command)
        LaunchRoot.startup(
            arguments,
            lambda: (
                LaunchRoot.from_path(args.root) if args.root else declaration.published_launch()
            ),
        )
        route = resolve_comms_route(args.root)
        with LaunchRoot.from_path(route.root).selected():
            command = declaration.from_arguments(args)
            return command.execute(route, sys.stdout)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, CoordinationError) as exc:
        return _fail(exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
