"""Declared native defaults passed explicitly through the public JSON codec."""

from __future__ import annotations

import argparse
import json
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from typing import Any, TYPE_CHECKING, ClassVar

from .field_codec import FieldCodec
from .pi_vocabulary import HighThinkingLevel, ThinkingLevel

if TYPE_CHECKING:
    from .owner_launch import LaunchRuntime


@dataclass(frozen=True)
class NativeThreadDefaults:
    """Creation defaults and one owner's explicit native launch selection."""

    binary: str = field(metadata={"flags": ("--agent-bin",), "parse": str})
    arguments: tuple[str, ...] = field(default=(), metadata={
        "flags": ("--agent-args",), "parse": lambda value: tuple(shlex.split(value)),
    })
    model: str = "openai-codex/gpt-6.1-sol"
    thinking_level: type[ThinkingLevel] = HighThinkingLevel
    value_flag: ClassVar[str] = "--native-settings"

    @classmethod
    def from_runtime(cls, runtime: LaunchRuntime) -> NativeThreadDefaults:
        """Bind creation defaults to the caller's already selected installation."""
        return cls(binary=runtime.native_entrypoint)

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
    def add_override_arguments(cls, parser: argparse.ArgumentParser) -> None:
        for declared in fields(cls):
            if "flags" in declared.metadata:
                parser.add_argument(*declared.metadata["flags"], dest=declared.name,
                                    type=declared.metadata["parse"], default=argparse.SUPPRESS)

    @classmethod
    def cli_overrides(cls, namespace: argparse.Namespace, defaults: NativeThreadDefaults) -> dict[str, Any]:
        selected = namespace.native_settings
        values = {} if selected is defaults else {declared.name: getattr(selected, declared.name)
                                                 for declared in fields(cls)}
        values.update((declared.name, vars(namespace)[declared.name]) for declared in fields(cls)
                      if "flags" in declared.metadata and declared.name in vars(namespace))
        return values

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
