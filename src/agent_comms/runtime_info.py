"""Runtime observations retain independent field provenance; reads never invent a birth."""
from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar, Literal, cast

from .context_reading import ContextReading, UnmeasuredContext
from .nominal.declared_family import DeclaredFamily
from .nominal.field_codec import FieldCodec
from .locked_store import DocumentChange
from .maintenance_barrier import CarriedDocument
from .pi_counts import PiCount, UnreportedCount
from .reading_attribution import CurrentReadingAttribution, ReadingAttribution
from .runtime_field import RuntimeField, RuntimePublication, UnobservedRuntimeField
from .store_files import DependentStateTier, SingleFileStore
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation, UnrecordedThreadIncarnation
from .thread_model import ThreadModel, UndeclaredModel
from .thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from .agent_events import RuntimeInfoObservation
    from .registry_document import RegistrySnapshot
    from .threads import Thread


class RuntimeInfo(DeclaredFamily, affix="RuntimeInfo"):
    """A displayed reading is not current-owner authorization."""

    model: ThreadModel
    context_used: PiCount
    context_size: PiCount

    @abstractmethod
    def context_reading(self) -> ContextReading: ...

    def current_model(self, owner: RecordedThreadIncarnation) -> ThreadModel:
        return UndeclaredModel()

    def model_attribution(self) -> ReadingAttribution:
        return CurrentReadingAttribution()


@dataclass(frozen=True, slots=True)
class UnobservedRuntimeInfo(RuntimeInfo):
    """No publication exists; do not invent a value or an observation clock."""

    model = UndeclaredModel()
    context_used = context_size = UnreportedCount()

    def context_reading(self) -> ContextReading:
        return UnmeasuredContext()


@dataclass(frozen=True, slots=True)
class AgentRuntimeInfo(RuntimeInfo):
    """Each independently published field owns its original source and clock."""

    model_observation: RuntimeField[ThreadModel] = field(default_factory=UnobservedRuntimeField)
    context_used_observation: RuntimeField[PiCount] = field(default_factory=UnobservedRuntimeField)
    context_size_observation: RuntimeField[PiCount] = field(default_factory=UnobservedRuntimeField)

    @property
    def model(self) -> ThreadModel:
        return self.model_observation.value_or(UndeclaredModel())

    @property
    def context_used(self) -> PiCount:
        return self.context_used_observation.value_or(UnreportedCount())

    @property
    def context_size(self) -> PiCount:
        return self.context_size_observation.value_or(UnreportedCount())

    def _fields(self) -> tuple[RuntimeField[object], ...]:
        return tuple(cast(RuntimeField[object], plan.current(self))
                     for plan in FieldCodec.field_plans(AgentRuntimeInfo))

    @property
    def timestamp(self) -> float:
        return max(observation.recorded_at() for observation in self._fields())

    def current_model(self, owner: RecordedThreadIncarnation) -> ThreadModel:
        return self.model_observation.current_value(owner, UndeclaredModel())

    def model_attribution(self) -> ReadingAttribution:
        return self.model_observation.attribution()

    def context_reading(self) -> ContextReading:
        attribution = self.context_used_observation.attribution().joined(
            self.context_size_observation.attribution())
        return replace(self.context_size.reading(self.context_used), attribution=attribution)

    def over(self, older: AgentRuntimeInfo) -> AgentRuntimeInfo:
        """Only published fields replace older ones, including published unknowns."""
        return FieldCodec.construct(type(self), {
            plan.name: cast(RuntimeField[object], plan.current(self)).over(
                cast(RuntimeField[object], plan.current(older)))
            for plan in FieldCodec.field_plans(AgentRuntimeInfo)
        })

    def as_read(self) -> RuntimeInfo:
        answer: RuntimeInfo = UnobservedRuntimeInfo()
        for observation in self._fields():
            answer = observation.snapshot_or(self, answer)
        return answer


@dataclass(frozen=True, slots=True)
class RuntimeReadScope:
    """An attested requested birth and its captured, display-only name lineage."""

    owner: RecordedThreadIncarnation
    names: tuple[str, ...]

    @classmethod
    def captured(cls, thread: Thread, snapshot: RegistrySnapshot) -> RuntimeReadScope:
        return cls(thread.incarnation.require_recorded(), tuple(sorted(snapshot.names_for(thread.name))))

    def unrecorded(self) -> tuple[UnrecordedThreadIncarnation, ...]:
        return tuple(UnrecordedThreadIncarnation(name)
                     for name in dict.fromkeys((self.owner.name, *self.names)))


@dataclass(frozen=True, slots=True)
class RuntimeInfoDocument:
    """Recorded and birthless scopes remain distinct native nominal keys."""

    observations: dict[ThreadIncarnation, AgentRuntimeInfo] = field(default_factory=dict)
    version: Literal[2] = 2

    def read_scope(self, scope: RuntimeReadScope) -> RuntimeInfo:
        historical = AgentRuntimeInfo()
        candidates = sorted(scope.unrecorded(), key=lambda owner: (
            owner.name == scope.owner.name,
            self.observations.get(owner, AgentRuntimeInfo()).timestamp,
        ))
        for owner in candidates:
            historical = self.observations.get(owner, AgentRuntimeInfo()).over(historical)
        return self.observations.get(scope.owner, AgentRuntimeInfo()).over(historical).as_read()


@dataclass(frozen=True, slots=True)
class RuntimeInfoStore(SingleFileStore, ThreadOwnedState, CarriedDocument[RuntimeInfoDocument], DependentStateTier):
    """A rename changes neither a recorded birth's key nor historical evidence."""

    filename: ClassVar[str] = "runtime_info.json"
    json_indent = 2
    record_type = RuntimeInfoDocument

    def empty(self) -> RuntimeInfoDocument:
        return RuntimeInfoDocument()

    def for_thread(self, scope: RuntimeReadScope) -> RuntimeInfo:
        return self.for_threads((scope,))[scope.owner]

    def for_threads(self, scopes: Iterable[RuntimeReadScope]) -> dict[RecordedThreadIncarnation, RuntimeInfo]:
        document = self.read()
        return {scope.owner: document.read_scope(scope) for scope in scopes}

    def observe(self, owner: RecordedThreadIncarnation, timestamp: float, observation: RuntimeInfoObservation) -> None:
        publication = RuntimePublication(owner, timestamp)

        def apply(document: RuntimeInfoDocument) -> DocumentChange[RuntimeInfoDocument, RuntimeInfoDocument]:
            values = document.observations
            current = values.get(owner, AgentRuntimeInfo())
            observed = observation.project_runtime_info(current, publication)
            return DocumentChange.to(RuntimeInfoDocument({**values, owner: observed}))

        self.update(apply)

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        owners = {thread.incarnation for thread in threads}
        self.update(lambda document: DocumentChange.to(RuntimeInfoDocument({
            owner: value for owner, value in document.observations.items() if owner not in owners
        }) if owners & document.observations.keys() else document))
