"""Capture an existing worker's original interpreter, environment and identity."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .child_process import Platform, ProcessIdentity
from .errors import RelationViolationError
from . import launch_runtime
from .native_settings import NativeThreadDefaults
from .registry_document import RegistrySnapshot
from .threads import Thread
from .thread_identity import OwnerIdentity
from .restart_refusals import OwnerGenerationChangedRefusal, OwnerSelectionChangedRefusal


@dataclass(frozen=True, slots=True)
class RetainedOwnerLaunch:
    """An immutable original process observation, not owner/admission authority."""

    process: ProcessIdentity
    interpreter: str
    environment: dict[str, str] = field(repr=False)
    native: NativeThreadDefaults
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
        command = tuple(os.fsdecode(token) for token in
                        Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0") if token)
        observed_interpreter = command[0] if command else ""
        if not observed_interpreter or (
            interpreter is not None and observed_interpreter != interpreter
        ):
            raise RelationViolationError("Selected source owner interpreter changed")
        raw = Path(f"/proc/{process.pid}/environ").read_bytes()
        values = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
        environment = {os.fsdecode(key): os.fsdecode(value) for key, value in values.items()}
        native = NativeThreadDefaults.from_arguments(command)
        platform.require(process)
        route = environment.get(launch_runtime.RestartEnvironment.owner_key)
        if not route:
            raise RelationViolationError("Owner launch configuration cannot be verified")
        # A worker's launch name may have been renamed since exec. The original
        # snapshot owns that relation; a PID or spelling match cannot grant it.
        if snapshot.owner_identity(route) != snapshot.owner_identity(thread.name):
            raise RelationViolationError("Owner launch names another registry incarnation")
        snapshot.require_owner_process(snapshot.owner_identity(thread.name), process)
        return cls(process, observed_interpreter, environment, native)


@dataclass(frozen=True, slots=True)
class OwnerRestartSelection:
    identity: OwnerIdentity
    process: ProcessIdentity
    admission_generation: int

    @classmethod
    def capture(cls, snapshot: RegistrySnapshot, name: str):
        owner = snapshot.require_active(name)
        return cls(
            snapshot.owner_identity(owner.name),
            owner.require_process(),
            snapshot.admission_generations[owner.name],
        )

    def recorded_native(self, snapshot: RegistrySnapshot) -> NativeThreadDefaults:
        return RetainedOwnerLaunch.capture(self.require_current(snapshot), snapshot).native

    @property
    def name(self) -> str:
        return self.identity.incarnation.name

    def require_current(self, snapshot: RegistrySnapshot) -> Thread:
        try:
            snapshot.require_owner_process(self.identity, self.process)
        except RelationViolationError as error:
            raise OwnerSelectionChangedRefusal() from error
        if snapshot.admission_generations[self.name] != self.admission_generation:
            raise OwnerGenerationChangedRefusal()
        return snapshot.require_active(self.name)
