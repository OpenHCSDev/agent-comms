"""OpenHCS agent communications — JSON CLI runtime execution."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import AbstractContextManager
from importlib import import_module
from typing import TextIO

from agent_comms.cli import CLI_DESCRIPTION, format_top_level_help, is_top_level_help
from agent_comms.cli_bridges.cli_arguments import CliArgumentParser, CliArguments, CliParserExit
from agent_comms.cli_bridges.cli_protocol import CoreError
from agent_comms.cli_support.cli_failures import CLI_FAILURES
from agent_comms.cli_verbs.cli_verb import CliBus
from agent_comms.execution.launch_root import LaunchRoot, started
from agent_comms.installer.active_route import CommsRoute, resolve_comms_route


def _open_bus(route: CommsRoute) -> AbstractContextManager[CliBus]:
    return import_module("agent_comms.comms").Comms.client(route)


def _fail(error: BaseException, output: TextIO) -> int:
    """The declared wire family owns classification, causes and JSON encoding."""
    print(CoreError.from_exception(error).model_dump_json(), file=output, flush=True)
    return 1


def build_parser(arguments: Sequence[str] = ()) -> argparse.ArgumentParser:
    parser = CliArgumentParser(prog="agent-comms", description=CLI_DESCRIPTION)
    parser.add_root_options()
    selection = parser.command_parsing(arguments)
    subparsers = parser.add_subparsers(required=True)
    for declaration in selection.declarations():
        declaration.add_parser(subparsers)
    return parser


class CliInvocation:
    """The CLI boundary shared by console entry and selected-runtime probes."""

    def execute(self, arguments: Sequence[str], output: TextIO) -> int:
        """Own parsing, root selection and dispatch for every registered command."""
        arguments = list(arguments)
        if is_top_level_help(arguments):
            output.write(format_top_level_help())
            return 0
        args, unknown = build_parser(arguments).parse_known_args(
            arguments, namespace=CliArguments())
        if unknown and (len(unknown) != 2 or unknown[0] != LaunchRoot.argument):
            raise ValueError(f"unrecognized arguments: {' '.join(unknown)}")
        declaration = args.command_declaration
        launch_root = LaunchRoot.startup(
            arguments,
            lambda: args.root.resolve(declaration.published_launch),
        )
        route = resolve_comms_route(args.root.resolve(lambda: launch_root).path)
        with LaunchRoot.from_path(route.root).selected():
            command = declaration.from_arguments(args)
            return command.execute(route, output, _open_bus)

    def invoke(self, arguments: Sequence[str], output: TextIO) -> int:
        """A call borrows its root; success, refusal and parser exit all restore it."""
        with LaunchRoot.unstarted().selected():
            try:
                return self.execute(arguments, output)
            except CLI_FAILURES as exc:
                return _fail(exc, output)
            except CliParserExit as exited:
                for text in exited.text.texts():
                    output.write(text)
                return exited.code


def main(argv: Sequence[str] | None = None) -> int:
    started()
    try:
        arguments = argv if isinstance(argv, Sequence) else sys.argv[1:]
        return CliInvocation().invoke(arguments, sys.stdout)
    except KeyboardInterrupt:
        return 130
