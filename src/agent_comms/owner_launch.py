"""Capture an existing worker's original interpreter, environment and identity."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .child_process import Platform, ProcessIdentity
from .errors import RelationViolationError
from . import launch_runtime
from .native_arguments import NativeArguments
from .registry_document import RegistrySnapshot
from .threads import Thread


@dataclass(frozen=True, slots=True)
class RetainedOwnerLaunch:
    """An immutable process observation, not ownership or permission to start work."""

    process: ProcessIdentity
    interpreter: str
    environment: dict[str, str] = field(repr=False)
    runtime: launch_runtime.LaunchRuntime

    @classmethod
    def capture(
        cls, thread: Thread, snapshot: RegistrySnapshot, *, interpreter: str | None = None
    ) -> RetainedOwnerLaunch:
        process = thread.require_process()
        platform = Platform.current()
        platform.require(process)
        command = Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0", 1)[0]
        observed_interpreter = os.fsdecode(command)
        if not observed_interpreter or (
            interpreter is not None and observed_interpreter != interpreter
        ):
            raise RelationViolationError("Selected source owner interpreter changed")
        raw = Path(f"/proc/{process.pid}/environ").read_bytes()
        values = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
        environment = {os.fsdecode(key): os.fsdecode(value) for key, value in values.items()}
        platform.require(process)
        runtime = launch_runtime.LaunchRuntime.for_process(process.pid)
        route = runtime.require_thread().name
        platform.require(process)
        # A worker's launch name may have been renamed since exec. The original
        # snapshot owns that relation; a PID or spelling match cannot grant it.
        if snapshot.owner_identity(route) != snapshot.owner_identity(thread.name):
            raise RelationViolationError("Owner launch names another registry incarnation")
        snapshot.require_owner_process(snapshot.owner_identity(thread.name), process)
        return cls(process, observed_interpreter, environment, runtime)
