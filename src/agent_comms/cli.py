"""OpenHCS agent communications — JSON CLI.

Commands print one JSON result, except watch, which streams messages as JSON
lines. Errors are one JSON line carrying a declared type and error text.
They exit 0 on success or 1 on failure. This is the adapter surface for the pi extension and other
process-based clients: they shell out and parse JSON, they do not own
orchestration semantics.
"""

from __future__ import annotations

from agent_comms.cli_arguments import CliArgumentParser, CliArguments

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from agent_comms.active_route import resolve_comms_route
from agent_comms.cli_failures import CLI_FAILURES
from agent_comms.cli_protocol import CoreError
from agent_comms.launch_root import LaunchRoot


def _fail(error: BaseException, output: TextIO) -> int:
    """The declared wire family owns classification, causes and JSON encoding."""
    print(CoreError.from_exception(error).model_dump_json(), file=output, flush=True)
    return 1


def build_parser(arguments: Sequence[str] = ()) -> argparse.ArgumentParser:
    parser = CliArgumentParser(prog="agent-comms", description=__doc__)
    parser.add_root_options()
    selection = CliArgumentParser.command_parsing(arguments)
    subparsers = parser.add_subparsers(required=True)
    for declaration in selection.declarations():
        declaration.add_parser(subparsers)
    return parser


class CliInvocation:
    """The CLI boundary shared by console entry and selected-runtime probes."""

    def execute(self, arguments: Sequence[str], output: TextIO) -> int:
        """Own parsing, root selection and dispatch for every registered command."""
        arguments = list(arguments)
        args, unknown = build_parser(arguments).parse_known_args(arguments, namespace=CliArguments())
        if unknown and (len(unknown) != 2 or unknown[0] != LaunchRoot.argument):
            raise ValueError(f"unrecognized arguments: {' '.join(unknown)}")
        declaration = args.command_declaration
        LaunchRoot.startup(
            arguments,
            lambda: args.root.resolve(declaration.published_launch),
        )
        route = resolve_comms_route(args.root.resolve(LaunchRoot.current).path)
        with LaunchRoot.from_path(route.root).selected():
            command = declaration.from_arguments(args)
            return command.execute(route, output)

    def invoke(self, arguments: Sequence[str], output: TextIO) -> int:
        """A call borrows its root; success, refusal and parser exit all restore it."""
        with LaunchRoot.unstarted().selected():
            try:
                return self.execute(arguments, output)
            except KeyboardInterrupt:
                return 130
            except CLI_FAILURES as exc:
                return _fail(exc, output)
            except SystemExit as exited:
                # CPython's process boundary accepts an integer, None, or error text.
                if isinstance(exited.code, int):
                    return exited.code
                if exited.code is None:
                    return 0
                print(exited.code, file=sys.stderr)
                return 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = argv if isinstance(argv, Sequence) else sys.argv[1:]
    return CliInvocation().invoke(arguments, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
