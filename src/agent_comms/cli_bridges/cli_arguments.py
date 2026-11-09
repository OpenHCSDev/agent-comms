"""CLI parsing keeps the selected declaration, not a second spelling of it."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Never, Self

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.execution.launch_root import DefaultLaunch, LaunchRequest, LaunchRoot
from agent_comms.nominal.field_codec import SubclassUnion
from agent_comms.registry.thread_text import AbsentText, GivenText, TextChoice

if TYPE_CHECKING:
    from agent_comms.cli_bridges.shared import BindingThread


class CommandParsing:
    """With no selected verb, prepare the complete command help."""

    def declarations(self) -> tuple[type[CliCommand], ...]:
        return CliCommand.members_with(CliCommand)


@dataclass(frozen=True)
class SelectedCommand(CommandParsing):
    declaration: type[CliCommand]

    @classmethod
    def from_name(cls, name: str) -> Self:
        return cls(CliCommand.member_named(name))

    def declarations(self) -> tuple[type[CliCommand], ...]:
        return (self.declaration,)


class _CommandParsingArguments(argparse.Namespace):
    command: str | None


class CliArguments(argparse.Namespace):
    root: LaunchRequest
    command: str
    command_declaration: type[CliCommand]
    binding_thread: BindingThread


class CliParserExit(SystemExit):
    """A parser exit carries its declared integer status, not arbitrary process state."""

    code: int
    text: CliParserText

    def __init__(self, status: int, text: CliParserText) -> None:
        super().__init__(status)
        self.text = text


class CliParserText(SubclassUnion, TextChoice, affix="CliParserText"):
    """The parser's text carried to the invocation that owns its output."""


@dataclass(frozen=True)
class GivenCliParserText(GivenText, CliParserText):
    text: str


@dataclass(frozen=True)
class NoCliParserText(AbsentText, CliParserText):
    pass


class CliArgumentParser(argparse.ArgumentParser):
    """Argument errors use Core's JSON failure boundary, including subcommands."""

    def print_help(self, file: object = None) -> Never:
        raise CliParserExit(0, GivenCliParserText(self.format_help()))

    def print_usage(self, file: object = None) -> Never:
        raise CliParserExit(0, GivenCliParserText(self.format_usage()))

    def add_root_options(self) -> None:
        self.add_argument(
            "--root", type=LaunchRoot.from_path, default=DefaultLaunch(),
            help="Explicit Comms root directory (otherwise uses the published active route)",
        )
        self.add_argument(LaunchRoot.argument, help=argparse.SUPPRESS)

    def command_parsing(self, arguments: Sequence[str]) -> CommandParsing:
        selection = type(self)(add_help=False)
        selection.add_root_options()
        # The verb is read here, not by argparse's `type=` hook: that hook rewrites whatever
        # its callable raises into "invalid <function> value", so an unknown verb was reported
        # as a bad caller name and the family's own UnknownFamilyMember survived only as a
        # cause, five clauses into the line.
        selection.add_argument("command", nargs="?", default=None, help=argparse.SUPPRESS)
        args, _ = selection.parse_known_args(arguments, namespace=_CommandParsingArguments())
        if args.command is None:
            return CommandParsing()
        return SelectedCommand.from_name(args.command)

    def exit(self, status: int = 0, message: object = None) -> Never:
        raise CliParserExit(status, NoCliParserText())

    def error(self, message: str) -> Never:
        raise ValueError(message)
