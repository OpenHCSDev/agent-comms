"""Shared command-line transport for FieldCodec records."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import ClassVar, Self

from .field_codec import DisclosedRecord, FieldCodec


class SingleArgument(argparse.Action):
    """A caller selection is one explicit value, never last-writer-wins authority."""

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                 values: object, option_string: object = argparse.SUPPRESS) -> None:
        if self.dest in vars(namespace):
            parser.error(f'{self.format_usage()} must be supplied only once')
        setattr(namespace, self.dest, values)


class JsonFlagRecord(DisclosedRecord):
    """A record supplies its JSON-value flag; FieldCodec owns its representation."""

    __slots__ = ()
    value_flag: ClassVar[str]

    def to_json(self) -> str:
        return json.dumps(FieldCodec.encode(self))

    @classmethod
    def from_json(cls, encoded: str | bytes) -> Self:
        return FieldCodec.decode(cls, json.loads(encoded))

    @property
    def cli_arguments(self) -> tuple[str, str]:
        return self.value_flag, self.to_json()

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser, *, required: bool, dest: str) -> None:
        parser.add_argument(
            cls.value_flag, type=cls.from_json, dest=dest, metavar="JSON",
            required=required, default=argparse.SUPPRESS,
        )

    @classmethod
    def from_arguments(cls, arguments: Sequence[str], *defaults: Self) -> Self:
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        cls.add_cli_arguments(parser, required=False, dest="record")
        if defaults:
            default, = defaults
            parser.set_defaults(record=default)
        namespace, _ = parser.parse_known_args(arguments)
        if "record" not in vars(namespace):
            raise ValueError(f"Missing required {cls.value_flag}")
        return namespace.record
