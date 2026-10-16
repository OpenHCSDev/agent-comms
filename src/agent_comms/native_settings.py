"""Declared native defaults and the immutable settings passed to an owner."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .field_codec import FieldCodec
from .child_process import Platform
from .pi_vocabulary import HighThinkingLevel, ThinkingLevel


@dataclass(frozen=True)
class NativeThreadDefaults:
    """Creation defaults and an owner's explicit native launch selection.

    The launch descriptor is anonymous, sealed and inherited by the child. It
    lives exactly as long as that process; argv need not disclose argument values
    and restart capture never consults the operator's environment for them.
    """

    binary: str
    arguments: tuple[str, ...] = ()
    model: str = "openai-codex/gpt-6.1-sol"
    thinking_level: type[ThinkingLevel] = HighThinkingLevel
    descriptor_flag: ClassVar[str] = "--native-settings-fd"
    value_flag: ClassVar[str] = "--native-settings"

    @staticmethod
    def runtime_binary() -> str:
        """Bind the launcher to this exact installed interpreter, not a moving link."""
        return str(Path(sys.executable).with_name("pi-comms-native").resolve())

    def __post_init__(self) -> None:
        if not isinstance(self.binary, str) or not self.binary:
            raise ValueError("A native binary is required")
        if not isinstance(self.arguments, tuple) or any(
            not isinstance(argument, str) for argument in self.arguments
        ):
            raise ValueError("Native arguments must be a tuple of strings")

    @classmethod
    def from_runtime(cls) -> NativeThreadDefaults:
        """Bind declared defaults to the running installation at the entrypoint."""
        return cls(binary=cls.runtime_binary())

    @contextmanager
    def inherited(self) -> Iterator[tuple[tuple[str, ...], tuple[int, ...]]]:
        """Lend the existing process launcher an immutable private descriptor."""
        with Platform.current().inherited_payload(self.to_json().encode("utf-8")) as descriptor:
            yield (self.descriptor_flag, str(descriptor)), (descriptor,)

    @classmethod
    def add_launch_argument(cls, parser: argparse.ArgumentParser, *, required: bool) -> None:
        parser.add_argument(cls.descriptor_flag, type=int, required=required,
                            help="Inherited immutable native launch settings descriptor")

    @classmethod
    def from_descriptor(cls, descriptor: int) -> NativeThreadDefaults:
        return cls.from_json(Platform.current().read_inherited_payload(descriptor))

    def to_json(self) -> str:
        return json.dumps(FieldCodec.encode(self))

    @classmethod
    def from_json(cls, encoded: str | bytes) -> NativeThreadDefaults:
        return FieldCodec.decode(cls, json.loads(encoded))

    @property
    def cli_arguments(self) -> tuple[str, str]:
        """Explicit public CLI selection; never put credentials in argv."""
        return self.value_flag, self.to_json()

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        group = parser.add_mutually_exclusive_group()
        group.add_argument(cls.value_flag, type=cls.from_json, dest="native_settings", metavar="JSON",
                           default=argparse.SUPPRESS,
                           help="Explicit typed native launch defaults (not credentials)")
        group.add_argument(cls.descriptor_flag, type=lambda value: cls.from_descriptor(int(value)),
                           dest="native_settings", metavar="FD", default=argparse.SUPPRESS,
                           help="Inherited private native launch settings descriptor")

    @classmethod
    def from_namespace(cls, namespace: argparse.Namespace) -> NativeThreadDefaults:
        if hasattr(namespace, "native_settings"):
            return namespace.native_settings
        return cls.from_runtime()

    @classmethod
    def from_cli(cls, arguments: Sequence[str]) -> NativeThreadDefaults:
        parser = argparse.ArgumentParser(add_help=False)
        cls.add_cli_arguments(parser)
        namespace, _ = parser.parse_known_args(arguments)
        return cls.from_namespace(namespace)

    @classmethod
    def from_process(cls, pid: int, command: Sequence[str]) -> NativeThreadDefaults:
        """Read only the exact original process's declared immutable selection."""
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        cls.add_launch_argument(parser, required=False)
        try:
            namespace, _ = parser.parse_known_args(command)
        except (argparse.ArgumentError, SystemExit) as error:
            raise ValueError("Original owner has no typed native launch settings") from error
        if namespace.native_settings_fd is None:
            raise ValueError("Original owner has no typed native launch settings")
        path = Path(f"/proc/{pid}/fd/{namespace.native_settings_fd}")
        with path.open("rb") as stream:
            return cls.from_descriptor(stream.fileno())
