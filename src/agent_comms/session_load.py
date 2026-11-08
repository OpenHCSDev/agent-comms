"""The declared session type checks owner permission when handling an ACP load."""

from __future__ import annotations
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from functools import partial
from .coordination.work import CoordinationWork
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .nominal.declared_family import DeclaredFamily
from .errors import RelationViolationError
from .nominal.field_codec import FieldCodec
import agent_comms.thread_presentation as thread_presentation
from .native_arguments import GivenArguments

if TYPE_CHECKING:
    from .execution.session_lifecycle import AttachedSessionLifecycle
    from .registry.threads import Thread


class SessionLoadPlan(CoordinationWork, DeclaredFamily, affix='SessionLoadPlan'):
    @classmethod
    def at_ingress(cls, metadata: object) -> 'SessionLoadPlan':
        return EnsuringSessionLoadPlan() if metadata is None else FieldCodec.decode(cls, metadata)

    def metadata(self) -> dict:
        return {"agentCommsLoad": FieldCodec.encode(self)}

    @staticmethod
    def witnessed(metadata: object, key: str, witness: type[SessionLoadPlan]) -> SessionLoadPlan:
        """Foreign metadata carries a load witness under its key, or carries none."""
        if isinstance(metadata, dict) and key in metadata:
            return FieldCodec.decode(witness, metadata[key])
        return EnsuringSessionLoadPlan()

    @classmethod
    def at_response(cls, metadata: object) -> 'SessionLoadPlan':
        return cls.witnessed(metadata, "agentCommsLoad", ExistingSessionLoadPlan)

    @classmethod
    def from_failure(cls, data: object) -> SessionLoadPlan:
        """Decode foreign failure metadata into a plan with its own reload behavior."""
        return cls.witnessed(data, "agentCommsLoadFailure", FailedSessionLoadPlan)

    def superseded_by(self, binding: thread_presentation.ThreadOwnerBinding) -> bool:
        return False

    async def reload_if_superseded(
        self, binding: thread_presentation.LiveThreadOwnerBinding,
        reload: Callable[[SessionLoadPlan], Awaitable[None]],
    ) -> None:
        """A plan without an owner witness cannot request an observed reload."""

    @abstractmethod
    async def resolve(self, lifecycle: "AttachedSessionLifecycle", thread: "Thread") -> "Thread": ...


@dataclass(frozen=True, slots=True)
class EnsuringSessionLoadPlan(SessionLoadPlan):
    async def resolve(self, lifecycle: "AttachedSessionLifecycle", thread: "Thread") -> "Thread":
        return await self.run_worker(partial(
            lifecycle.comms.owners.ensure_owner, thread.name,
            agent_bin=lifecycle.effects.inputs.configuration.launch.binary,
            agent_args=GivenArguments(lifecycle.effects.inputs.configuration.agent_args.argv),
        ))


@dataclass(frozen=True, slots=True)
class WitnessedSessionLoadPlan(SessionLoadPlan):
    """Original load command/response evidence, never current registry state."""

    binding: thread_presentation.ThreadOwnerBinding

    def superseded_by(self, binding: thread_presentation.ThreadOwnerBinding) -> bool:
        return self.binding.superseded_by(binding)

    async def reload_if_superseded(
        self, binding: thread_presentation.LiveThreadOwnerBinding,
        reload: Callable[[SessionLoadPlan], Awaitable[None]],
    ) -> None:
        if self.superseded_by(binding):
            await reload(ExistingSessionLoadPlan(binding))


@dataclass(frozen=True, slots=True)
class ExistingSessionLoadPlan(WitnessedSessionLoadPlan):
    binding: thread_presentation.LiveThreadOwnerBinding

    async def resolve(self, lifecycle: "AttachedSessionLifecycle", thread: "Thread") -> "Thread":
        entry = (await self.run_worker(partial(
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

    def failure_metadata(self) -> dict:
        return {"agentCommsLoadFailure": FieldCodec.encode(self)}

    async def resolve(self, lifecycle: "AttachedSessionLifecycle", thread: "Thread") -> "Thread":
        raise RelationViolationError("A failed load command cannot be replayed")
