"""The declared session type checks owner permission when handling an ACP load."""
from abc import abstractmethod
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .launch_runtime import LaunchRecord
from .thread_presentation import LiveThreadOwnerBinding, ThreadOwnerBinding

if TYPE_CHECKING:
    from .session_lifecycle import AttachedSessionLifecycle
    from .threads import Thread


class SessionLoadPlan(DeclaredFamily, affix='SessionLoadPlan'):
    @classmethod
    def at_ingress(cls, metadata: object) -> 'SessionLoadPlan':
        return EnsuringSessionLoadPlan() if metadata is None else FieldCodec.decode(cls, metadata)

    def metadata(self) -> dict:
        return {"agentCommsLoad": FieldCodec.encode(self)}

    @classmethod
    def at_response(cls, metadata: object) -> 'SessionLoadPlan':
        if not isinstance(metadata, dict):
            return EnsuringSessionLoadPlan()
        witness = metadata.get("agentCommsLoad")
        return (EnsuringSessionLoadPlan() if witness is None
                else FieldCodec.decode(ExistingSessionLoadPlan, witness))

    def superseded_by(self, binding: ThreadOwnerBinding) -> bool:
        return False

    @abstractmethod
    async def resolve(self, lifecycle: "AttachedSessionLifecycle", thread: "Thread") -> "Thread": ...


@dataclass(frozen=True, slots=True)
class EnsuringSessionLoadPlan(SessionLoadPlan):
    async def resolve(self, lifecycle: "AttachedSessionLifecycle", thread: "Thread") -> "Thread":
        return await Coordination.run_worker(partial(
            lifecycle.comms.owners.ensure_owner, thread.name,
            record=replace(LaunchRecord.current(), agent_bin=lifecycle.agent_bin,
                           agent_args=lifecycle.agent_args.argv),
        ))


@dataclass(frozen=True, slots=True)
class WitnessedSessionLoadPlan(SessionLoadPlan):
    """Original load command/response evidence, never current registry state."""

    binding: ThreadOwnerBinding

    def superseded_by(self, binding: ThreadOwnerBinding) -> bool:
        return self.binding.superseded_by(binding)


@dataclass(frozen=True, slots=True)
class ExistingSessionLoadPlan(WitnessedSessionLoadPlan):
    binding: LiveThreadOwnerBinding

    async def resolve(self, lifecycle: "AttachedSessionLifecycle", thread: "Thread") -> "Thread":
        entry = (await Coordination.run_worker(partial(
            lifecycle.comms.registry.entry, thread.name,
        ))).entry
        if thread.incarnation != self.binding.owner.incarnation:
            raise RelationViolationError("Read-only attachment targets a different thread incarnation")
        current = entry.require_owner_process(self.binding.owner, self.binding.process)
        if not self.binding.process.alive():
            raise RelationViolationError("Read-only attachment owner exited; no owner was started")
        return current


@dataclass(frozen=True, slots=True)
class FailedSessionLoadPlan(WitnessedSessionLoadPlan):
    """The original failed load command's witness; never an input permission."""

    @classmethod
    def from_failure(cls, data: object) -> 'FailedSessionLoadPlan | None':
        if not isinstance(data, dict):
            return None
        witness = data.get("agentCommsLoadFailure")
        return None if witness is None else FieldCodec.decode(cls, witness)

    def failure_metadata(self) -> dict:
        return {"agentCommsLoadFailure": FieldCodec.encode(self)}

    async def resolve(self, lifecycle: "AttachedSessionLifecycle", thread: "Thread") -> "Thread":
        raise RelationViolationError("A failed load command cannot be replayed")
