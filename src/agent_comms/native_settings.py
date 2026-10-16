"""Declared native defaults passed explicitly through the public JSON codec."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .field_codec import FieldCodec
from .pi_vocabulary import HighThinkingLevel, ThinkingLevel


@dataclass(frozen=True)
class NativeThreadDefaults:
    """Creation defaults and one owner's explicit native launch selection."""

    binary: str
    arguments: tuple[str, ...] = ()
    model: str = "openai-codex/gpt-6.1-sol"
    thinking_level: type[ThinkingLevel] = HighThinkingLevel
    value_flag: ClassVar[str] = "--native-settings"

    @staticmethod
    def runtime_binary() -> str:
        """Pending integration with the launch runtime's selected entrypoint."""
        return str(Path(sys.executable).with_name("pi-comms-native").resolve())

    @classmethod
    def from_runtime(cls) -> NativeThreadDefaults:
        """Pending integration with the required typed launch runtime."""
        return cls(binary=cls.runtime_binary())

    def to_json(self) -> str:
        return json.dumps(FieldCodec.encode(self))

    @classmethod
    def from_json(cls, encoded: str | bytes) -> NativeThreadDefaults:
        return FieldCodec.decode(cls, json.loads(encoded))

    @property
    def cli_arguments(self) -> tuple[str, str]:
        return self.value_flag, self.to_json()

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser, *, required: bool) -> None:
        parser.add_argument(cls.value_flag, type=cls.from_json, dest="native_settings",
                            metavar="JSON", required=required, default=argparse.SUPPRESS,
                            help="Explicit typed native launch defaults")

    @classmethod
    def from_cli(cls, arguments: Sequence[str], settings: NativeThreadDefaults) -> NativeThreadDefaults:
        parser = argparse.ArgumentParser(add_help=False)
        parser.set_defaults(native_settings=settings)
        cls.add_cli_arguments(parser, required=False)
        namespace, _ = parser.parse_known_args(arguments)
        return namespace.native_settings

    @classmethod
    def from_command(cls, command: Sequence[str]) -> NativeThreadDefaults:
        """Decode the exact original process selection, never the operator's env."""
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        cls.add_cli_arguments(parser, required=True)
        try:
            namespace, _ = parser.parse_known_args(command)
        except (argparse.ArgumentError, SystemExit) as error:
            raise ValueError("Original owner has no typed native launch settings") from error
        return namespace.native_settings
