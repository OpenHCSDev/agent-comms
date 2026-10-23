"""Command families whose members declare their options as fields."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import ClassVar, Self

from agent_comms.declared_family import DeclaredFamily
from agent_comms.field_codec import FieldCodec


class DeclaredOptions:
    """The CLI adapter consumes FieldCodec's existing field plans."""
    help: ClassVar[str]

    @classmethod
    def add_parser[P: argparse.ArgumentParser](
            cls, subparsers: argparse._SubParsersAction[P]) -> None:
        cls.add_arguments(subparsers.add_parser(FieldCodec.encode(cls), help=cls.help))

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        FieldCodec.add_arguments(cls, parser)

    @classmethod
    def from_arguments(cls, args: argparse.Namespace) -> Self:
        return FieldCodec.decode_arguments(cls, args)


class DeclaredVerb(DeclaredOptions, DeclaredFamily, affix="Verb"):
    """A command family parses the options declared by its members."""

    @classmethod
    def tool(cls) -> str:
        return cls.__module__.rpartition('.')[2].replace('_', '-')

    @classmethod
    def parse(cls, argv: Sequence[str]) -> Self:
        parser = argparse.ArgumentParser(
            prog=cls.tool(), description=sys.modules[cls.__module__].__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        verbs = parser.add_subparsers(dest="command", required=True)
        for member in cls.members_with(cls):
            member.add_parser(verbs)
        args = parser.parse_args(argv)
        return cls.member_named(args.command).from_arguments(args)
