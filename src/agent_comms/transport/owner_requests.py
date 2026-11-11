from __future__ import annotations

import os
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
from typing import TYPE_CHECKING

from agent_comms.errors import RelationViolationError
from agent_comms.execution import launch_runtime
from agent_comms.execution.child_process import Platform, ProcessIdentity
from agent_comms.execution.native_arguments import (
    ConfiguredBinary,
    InheritedArguments,
    LaunchArguments,
    LaunchBinary,
    NativeArguments,
)
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.thread_identity import ClaimedIdentity, OwnerIdentity
from agent_comms.transport.owner_connection import OwnerConnection
from agent_comms.transport.runtime_request import AttachedResultRuntimeRequest, ResultRuntimeRequest
from agent_comms.transport.tool_result import DeclaredReply

if TYPE_CHECKING:
    from agent_comms.execution.input_drain import OwnerSession
    from agent_comms.execution.owner_lifecycle import OwnerRestartSelection
    from agent_comms.execution.session_lifecycle import SessionLifecycle


@dataclass(frozen=True, slots=True)
class RetainedOwnerLaunch(DeclaredReply):
    """Original session configuration, not ownership or permission to start work."""

    process: ProcessIdentity
    interpreter: str
    environment: dict[str, str] = field(repr=False)
    configuration: launch_runtime.RestartEnvironment = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "configuration", launch_runtime.RestartEnvironment.inherit(self.environment))

    @classmethod
    def configured(
        cls, binary: LaunchBinary = ConfiguredBinary(),
        arguments: LaunchArguments = InheritedArguments(),
    ) -> RetainedOwnerLaunch:
        """Capture a process ingress once, before its configuration is shared by reference."""
        environment = dict(os.environ)
        environment[NativeArguments.binary_key] = binary.configured(environment)
        arguments.apply(environment)
        return cls(ProcessIdentity.capture(os.getpid()),
                   str(launch_runtime.LaunchRuntime.running().interpreter), environment)

    @classmethod
    def capture(
        cls, selection: OwnerRestartSelection, snapshot: RegistrySnapshot, root: Path,
    ) -> RetainedOwnerLaunch:
        thread = selection.require_current(snapshot)
        process = selection.process
        platform = Platform.current()
        platform.require(process)
        result = FieldCodec.decode(cls, OwnerConnection.exchange(
            root, process, ReadOwnerLaunchRuntimeRequest(thread=thread.name, owner=selection.identity),
        ))
        if result.process != process or result.interpreter != selection.interpreter:
            raise RelationViolationError(
                f"Selected source owner interpreter changed: the owner reports {result.interpreter} "
                f"in {result.process}; its selection recorded {selection.interpreter} in {process}"
            )
        platform.require(process)
        route = launch_runtime.ThreadLaunch.current(result.environment).name
        if not result.environment.get(NativeArguments.binary_key):
            raise RelationViolationError("Owner launch configuration cannot be verified")
        # A worker's launch name may have been renamed since exec. The original
        # snapshot owns that relation; a PID or spelling match cannot grant it.
        if snapshot.owner_identity(route) != snapshot.owner_identity(thread.name):
            raise RelationViolationError("Owner launch names another registry incarnation")
        snapshot.require_owner_process(snapshot.owner_identity(thread.name), process)
        result.arguments  # Decode/validate malformed shell arguments before any fence.
        return result

    @property
    def binary(self) -> str:
        return self.environment[NativeArguments.binary_key]

    @property
    def arguments(self) -> tuple[str, ...]:
        return InheritedArguments().configured(self.environment).argv


@dataclass(frozen=True)
class OwnerRetiredReply(DeclaredReply):
    owner: OwnerIdentity


@dataclass(frozen=True, kw_only=True)
class ReadHostResourcesRuntimeRequest(AttachedResultRuntimeRequest):
    pass


@dataclass(frozen=True, kw_only=True)
class ReadOwnerLaunchRuntimeRequest(AttachedResultRuntimeRequest):
    owner: OwnerIdentity


@dataclass(frozen=True, kw_only=True)
class StartOwnerRuntimeRequest(ResultRuntimeRequest):
    launch: RetainedOwnerLaunch
    owner: OwnerIdentity


@dataclass(frozen=True, kw_only=True)
class StopOwnerRuntimeRequest(ResultRuntimeRequest):
    owner: OwnerIdentity
    claim: ClaimedIdentity

    async def retire(self, sessions: SessionLifecycle, state: OwnerSession) -> OwnerRetiredReply:
        if state.owner.incarnation != self.owner.incarnation or state.owner.generation >= self.owner.generation:
            raise RelationViolationError("Stop request does not fence this original session")
        await sessions.retire_owner(state, self.owner, self.claim)
        return OwnerRetiredReply(state.owner)
