"""Declaration-owned owner admission at the external ACP load boundary."""
from abc import abstractmethod
from functools import partial
from .coordinator import Coordination
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
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
            agent_bin=lifecycle.agent_bin, agent_args=list(lifecycle.agent_args.argv),
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
        snapshot = await Coordination.run_worker(lifecycle.comms.registry.snapshot)
        if thread.incarnation != self.binding.owner.incarnation:
            raise RelationViolationError("Read-only attachment targets a different thread incarnation")
        snapshot.require_owner_process(self.binding.owner, self.binding.process)
        if not self.binding.process.alive():
            raise RelationViolationError("Read-only attachment owner exited; no owner was started")
        return snapshot.require_active(thread.name)


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
