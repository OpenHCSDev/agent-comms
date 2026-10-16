"""Declared native defaults and the immutable settings passed to an owner."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

from .field_codec import FieldCodec
from .pi_vocabulary import HighThinkingLevel, ThinkingLevel


@dataclass(frozen=True)
class NativeThreadDefaults:
    """Creation defaults and an owner's explicit native launch selection.

    The launch descriptor is anonymous, sealed and inherited by the child. It
    lives exactly as long as that process; argv need not disclose argument values
    and restart capture never consults the operator's environment for them.
    """

    model: str = "openai-codex/gpt-6.1-sol"
    thinking_level: type[ThinkingLevel] = HighThinkingLevel
    binary: str = "pi"
    arguments: tuple[str, ...] = ()
    descriptor_flag: ClassVar[str] = "--native-settings-fd"

    @staticmethod
    def descriptor_seals() -> int:
        import fcntl

        return fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL

    def __post_init__(self) -> None:
        if not self.binary:
            raise ValueError("A native binary is required")
        if not isinstance(self.arguments, tuple) or any(
            not isinstance(argument, str) for argument in self.arguments
        ):
            raise ValueError("Native arguments must be a tuple of strings")

    def for_launch(
        self, binary: str | None, arguments: Sequence[str] | None,
    ) -> NativeThreadDefaults:
        """Bind explicit API overrides; absence retains the declared defaults."""
        return replace(
            self,
            binary=self.binary if binary is None else binary,
            arguments=self.arguments if arguments is None else tuple(arguments),
        )

    @contextmanager
    def inherited(self) -> Iterator[tuple[tuple[str, ...], tuple[int, ...]]]:
        """Lend the existing process launcher an immutable private descriptor."""
        import fcntl

        descriptor = os.memfd_create("native-settings", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(FieldCodec.encode(self)).encode("utf-8"))
            stream.flush()
            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, self.descriptor_seals())
            yield (self.descriptor_flag, str(descriptor)), (descriptor,)

    @classmethod
    def add_launch_argument(cls, parser: argparse.ArgumentParser, *, required: bool) -> None:
        parser.add_argument(cls.descriptor_flag, type=int, required=required,
                            help="Inherited immutable native launch settings descriptor")

    @classmethod
    def from_descriptor(cls, descriptor: int) -> NativeThreadDefaults:
        import fcntl

        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != cls.descriptor_seals():
            raise ValueError("Native launch settings are not sealed")
        encoded = os.pread(descriptor, os.fstat(descriptor).st_size, 0)
        return FieldCodec.decode(cls, json.loads(encoded))

    @classmethod
    def from_process(cls, pid: int, command: Sequence[str]) -> NativeThreadDefaults:
        """Read only the exact original process's declared immutable selection."""
        parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
        cls.add_launch_argument(parser, required=True)
        try:
            namespace, _ = parser.parse_known_args(command)
        except (argparse.ArgumentError, SystemExit) as error:
            raise ValueError("Original owner has no typed native launch settings") from error
        path = Path(f"/proc/{pid}/fd/{namespace.native_settings_fd}")
        with path.open("rb") as stream:
            return cls.from_descriptor(stream.fileno())
