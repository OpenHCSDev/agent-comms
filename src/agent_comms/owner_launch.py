"""Capture an existing worker's original interpreter, environment and identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .child_process import Platform, ProcessIdentity
from .errors import RelationViolationError
from . import launch_runtime
from .native_arguments import InheritedArguments, NativeArguments
from .registry_document import RegistrySnapshot

if TYPE_CHECKING:
    from .owner_lifecycle import OwnerRestartSelection


@dataclass(frozen=True, slots=True)
class RetainedOwnerLaunch:
    """An immutable process observation, not ownership or permission to start work."""

    process: ProcessIdentity
    interpreter: str
    environment: dict[str, str] = field(repr=False)
    configuration: launch_runtime.RestartEnvironment = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "configuration", launch_runtime.RestartEnvironment.inherit(self.environment))

    @classmethod
    def capture(
        cls, selection: OwnerRestartSelection, snapshot: RegistrySnapshot
    ) -> RetainedOwnerLaunch:
        thread = selection.require_current(snapshot)
        process = selection.process
        platform = Platform.current()
        platform.require(process)
        observed_interpreter = process.arguments()[0]
        if observed_interpreter != selection.interpreter:
            raise RelationViolationError("Selected source owner interpreter changed")
        environment = process.environment()
        platform.require(process)
        route = launch_runtime.ThreadLaunch.current(environment).name
        if not environment.get(NativeArguments.binary_key):
            raise RelationViolationError("Owner launch configuration cannot be verified")
        # A worker's launch name may have been renamed since exec. The original
        # snapshot owns that relation; a PID or spelling match cannot grant it.
        if snapshot.owner_identity(route) != snapshot.owner_identity(thread.name):
            raise RelationViolationError("Owner launch names another registry incarnation")
        snapshot.require_owner_process(snapshot.owner_identity(thread.name), process)
        result = cls(process, observed_interpreter, environment)
        result.arguments  # Decode/validate malformed shell arguments before any fence.
        return result

    @property
    def binary(self) -> str:
        return self.environment[NativeArguments.binary_key]

    @property
    def arguments(self) -> tuple[str, ...]:
        return InheritedArguments().configured(self.environment).argv
