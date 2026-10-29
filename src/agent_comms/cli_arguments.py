"""CLI parsing keeps the selected declaration, not a second spelling of it."""

from __future__ import annotations

import argparse
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Never, Self, TextIO

from agent_comms.cli_command import CliCommand
from agent_comms.launch_root import DefaultLaunch, LaunchRequest, LaunchRoot
from agent_comms.field_codec import FieldCodec, SubclassUnion
from agent_comms.thread_text import AbsentText, GivenText, TextChoice

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
    command: CommandParsing


class CliArguments(argparse.Namespace):
    root: LaunchRequest
    command: str
    command_declaration: type[CliCommand]
    binding_thread: BindingThread


class CliParserExit(SystemExit):
    """A parser exit carries its declared integer status, not arbitrary process state."""

    code: int


class CliParserText(SubclassUnion, TextChoice, affix="CliParserText"):
    """Argparse's printed text, decoded once at the parser boundary."""


@dataclass(frozen=True)
class GivenCliParserText(GivenText, CliParserText):
    text: str


@dataclass(frozen=True)
class NoCliParserText(AbsentText, CliParserText):
    pass


class CliArgumentParser(argparse.ArgumentParser, ABC):
    """Argument errors use Core's JSON failure boundary, including subcommands."""

    @property
    @abstractmethod
    def output(self) -> TextIO: ...

    @staticmethod
    def writing_to(stream: TextIO) -> type[CliArgumentParser]:
        class WritingParser(CliArgumentParser):
            output = stream

        return WritingParser

    def _print_message(self, message: object, file: object = None) -> None:
        for text in FieldCodec.decode(CliParserText, message).texts():
            self.output.write(text)

    def add_root_options(self) -> None:
        self.add_argument(
            "--root", type=LaunchRoot.from_path, default=DefaultLaunch(),
            help="Explicit Comms root directory (otherwise uses the published active route)",
        )
        self.add_argument(LaunchRoot.argument, help=argparse.SUPPRESS)

    @classmethod
    def command_parsing(cls, arguments: Sequence[str]) -> CommandParsing:
        selection = cls(add_help=False)
        selection.add_root_options()
        selection.add_argument(
            "command", nargs="?", type=SelectedCommand.from_name, default=CommandParsing(),
        )
        args, _ = selection.parse_known_args(arguments, namespace=_CommandParsingArguments())
        return args.command

    def exit(self, status: int = 0, message: object = None) -> Never:
        self._print_message(message)
        raise CliParserExit(status)

    def error(self, message: str) -> Never:
        raise ValueError(message)
