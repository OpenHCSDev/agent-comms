"""Runtime metadata observations and their shared consumer, independent of turn execution."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from functools import partial

from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.coordination.work import CoordinationWork
from agent_comms.execution.agent_event import AgentEvent
from agent_comms.execution.pi_vocabulary import NoThinkingOverride, ThinkingLevelField
from agent_comms.execution.runtime_info import AgentRuntimeInfo
from agent_comms.execution.runtime_info_projection import RuntimeInfoProjection
from agent_comms.execution.runtime_info_source import RuntimeInfoSource
from agent_comms.nominal.field_codec import NullMember, boundary_decoder
from agent_comms.pi_counts import PiCount, UnreportedCount
from agent_comms.registry.thread_model import ThreadModel, UndeclaredModel


class RuntimeInfoObservation(AgentEvent, RuntimeInfoProjection[AgentRuntimeInfo]):
    """Record one producer observation through the existing runtime-info owner."""

    def project_runtime_info(self, current: AgentRuntimeInfo) -> AgentRuntimeInfo:
        return current

    async def record_in[Client: RuntimeInfoSource[RuntimeInfoObservation]](
        self, consumer: AgentEventConsumer[Client],
    ) -> None:
        await CoordinationWork.run_worker(partial(
            consumer.comms.agents.observe_runtime_info, consumer.thread_name, self,
        ))


@dataclass(frozen=True, kw_only=True)
class ContextUsageObservation(RuntimeInfoObservation):
    """Observe usage without claiming a model, identity or context window."""

    context_used: PiCount = field(default_factory=UnreportedCount)

    def project_runtime_info(self, current: AgentRuntimeInfo) -> AgentRuntimeInfo:
        return replace(super().project_runtime_info(current), context_used=self.context_used)


@dataclass(frozen=True, kw_only=True)
class ContextSizeObservation(RuntimeInfoObservation):
    """Observe the context window without restating unobserved usage."""

    context_size: PiCount = field(default_factory=UnreportedCount)

    def project_runtime_info(self, current: AgentRuntimeInfo) -> AgentRuntimeInfo:
        return replace(super().project_runtime_info(current), context_size=self.context_size)


@dataclass(frozen=True, kw_only=True)
class ContextInfo(ContextUsageObservation, ContextSizeObservation):
    """A usage snapshot carries both usage and the context window."""


@dataclass(frozen=True, kw_only=True)
class ModelObservation(RuntimeInfoObservation):
    """Observe the model that answers the thread."""

    model: ThreadModel = UndeclaredModel()

    def project_runtime_info(self, current: AgentRuntimeInfo) -> AgentRuntimeInfo:
        return replace(super().project_runtime_info(current), model=self.model)


@dataclass(frozen=True, kw_only=True)
class TranscriptModelUsage(ModelObservation, ContextUsageObservation):
    """A CLI transcript's answer names its model and context usage, never the window."""


class NativeSessionPathObservation(ABC):
    """A reported runtime path is evidence, never authority to replace its turn."""

    @classmethod
    @boundary_decoder
    def from_native(cls, path: str | None) -> NativeSessionPathObservation:
        return UnobservedNativeSessionPath() if path is None else ReportedNativeSessionPath(path)

    @abstractmethod
    async def attach(self, attachment: Callable[[str], Awaitable[None]]) -> None:
        """Apply only this observation through the original attachment owner."""


@dataclass(frozen=True)
class UnobservedNativeSessionPath(NullMember, NativeSessionPathObservation):
    async def attach(self, attachment: Callable[[str], Awaitable[None]]) -> None:
        pass


@dataclass(frozen=True)
class ReportedNativeSessionPath(NativeSessionPathObservation):
    path: str

    async def attach(self, attachment: Callable[[str], Awaitable[None]]) -> None:
        if self.path:
            await attachment(self.path)


@dataclass(frozen=True, kw_only=True)
class NativeMetadataObservation(ModelObservation):
    """Only native state snapshots carry identity/configuration observations."""

    session_file: NativeSessionPathObservation = UnobservedNativeSessionPath()
    thinking_level: ThinkingLevelField = NoThinkingOverride

    async def record_in[Client: RuntimeInfoSource[RuntimeInfoObservation]](
        self, consumer: AgentEventConsumer[Client],
    ) -> None:
        await consumer.before_native_metadata(self)
        await super().record_in(consumer)
        await consumer.after_native_metadata(self)


@dataclass(frozen=True, kw_only=True)
class AgentInfo(NativeMetadataObservation, ContextSizeObservation):
    """Native state observes metadata and context size, never context usage."""


class AgentEventConsumer[Client: RuntimeInfoSource[RuntimeInfoObservation]](MroDispatch):
    """Shared activity and metadata algorithms; owners supply their context."""

    def __init__(self, *, comms: Client, thread_name: str):
        self._comms = comms
        self._thread_name = thread_name

    @property
    def comms(self) -> Client:
        return self._comms

    @property
    def thread_name(self) -> str:
        return self._thread_name

    async def before_native_metadata(self, event: NativeMetadataObservation) -> None:
        pass

    async def after_native_metadata(self, event: NativeMetadataObservation) -> None:
        pass

    @handles(RuntimeInfoObservation)
    async def record_runtime_info(self, event: RuntimeInfoObservation) -> None:
        await event.record_in(self)
