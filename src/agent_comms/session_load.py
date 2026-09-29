"""Declaration-owned owner admission at the external ACP load boundary."""
from abc import abstractmethod
import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .thread_presentation import LiveThreadOwnerBinding, ThreadOwnerBinding

if TYPE_CHECKING:
    from .session_lifecycle import AttachedSessionLifecycle
    from .threads import Thread


class SessionLoadAdmission(DeclaredFamily, affix="SessionLoadAdmission"):
    @classmethod
    def at_ingress(cls, metadata: object) -> "SessionLoadAdmission":
        return EnsuringSessionLoadAdmission() if metadata is None else FieldCodec.decode(cls, metadata)

    def metadata(self) -> dict:
        return {"agentCommsLoad": FieldCodec.encode(self)}

    def already_requested(self, binding: ThreadOwnerBinding) -> bool:
        return False

    @abstractmethod
    async def resolve(self, lifecycle: "AttachedSessionLifecycle", thread: "Thread") -> "Thread": ...


@dataclass(frozen=True, slots=True)
class EnsuringSessionLoadAdmission(SessionLoadAdmission):
    async def resolve(self, lifecycle: "AttachedSessionLifecycle", thread: "Thread") -> "Thread":
        return await asyncio.to_thread(
            lifecycle.comms.owners.ensure_owner, thread.name,
            agent_bin=lifecycle.agent_bin, agent_args=list(lifecycle.agent_args.argv),
        )


@dataclass(frozen=True, slots=True)
class ExistingSessionLoadAdmission(SessionLoadAdmission):
    binding: LiveThreadOwnerBinding

    def already_requested(self, binding: ThreadOwnerBinding) -> bool:
        return self.binding == binding

    async def resolve(self, lifecycle: "AttachedSessionLifecycle", thread: "Thread") -> "Thread":
        snapshot = await asyncio.to_thread(lifecycle.comms.registry.snapshot)
        if thread.incarnation != self.binding.owner.incarnation:
            raise RelationViolationError("Read-only attachment targets a different thread incarnation")
        snapshot.require_owner_process(self.binding.owner, self.binding.process)
        if not self.binding.process.alive():
            raise RelationViolationError("Read-only attachment owner exited; no owner was started")
        return snapshot.require_active(thread.name)
