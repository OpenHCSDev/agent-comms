"""Capture an existing worker's original interpreter, environment and identity."""

from __future__ import annotations

import os
import argparse
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .child_process import Platform, ProcessIdentity
from .errors import RelationViolationError
from . import launch_runtime
from .native_arguments import NativeArguments
from .registry_document import RegistrySnapshot
from .threads import Thread
from .thread_identity import ThreadIncarnation


@dataclass(frozen=True, slots=True)
class RetainedOwnerLaunch:
    """An immutable original process observation, not owner/admission authority."""

    process: ProcessIdentity
    interpreter: str
    environment: dict[str, str] = field(repr=False)
    configuration: launch_runtime.RestartEnvironment = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "configuration", launch_runtime.RestartEnvironment.inherit(self.environment))

    @classmethod
    def capture(
        cls, thread: Thread, snapshot: RegistrySnapshot, *, interpreter: str | None = None
    ) -> RetainedOwnerLaunch:
        process = thread.require_process()
        platform = Platform.current()
        platform.require(process)
        command = [os.fsdecode(value) for value in Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0") if value]
        observed_interpreter = command[0] if command else ""
        if not observed_interpreter or (
            interpreter is not None and observed_interpreter != interpreter
        ):
            raise RelationViolationError("Selected source owner interpreter changed")
        raw = Path(f"/proc/{process.pid}/environ").read_bytes()
        values = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
        environment = {os.fsdecode(key): os.fsdecode(value) for key, value in values.items()}
        platform.require(process)
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument(ThreadIncarnation.value_flag, type=ThreadIncarnation.from_json,
                            dest="incarnation", required=True)
        try:
            selected, _ = parser.parse_known_args(command[1:])
        except SystemExit as error:
            raise RelationViolationError("Owner launch has no valid recorded caller") from error
        if not environment.get(NativeArguments.binary_key):
            raise RelationViolationError("Owner launch configuration cannot be verified")
        # A worker's launch name may have been renamed since exec. The original
        # snapshot owns that relation; a PID or spelling match cannot grant it.
        if not selected.incarnation.current(snapshot) or selected.incarnation.resolved(snapshot) != thread.incarnation:
            raise RelationViolationError("Owner launch names another registry incarnation")
        snapshot.require_owner_process(snapshot.owner_identity(thread.name), process)
        result = cls(process, observed_interpreter, environment)
        result.arguments  # Decode/validate malformed shell arguments before any fence.
        return result

    @property
    def binary(self) -> str:
        return self.environment[NativeArguments.binary_key]

    @property
    def arguments(self) -> tuple[str, ...] | None:
        arguments = self.environment.get(NativeArguments.arguments_key)
        return tuple(shlex.split(arguments)) if arguments is not None else None
