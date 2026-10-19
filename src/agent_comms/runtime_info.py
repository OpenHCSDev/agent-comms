"""Runtime info: declaration and persistence owners."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar

from .context_reading import ContextReading, UnmeasuredContext
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .locked_store import LockedStore, RootFile
from .pi_counts import PiCount, UnreportedCount
from .thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from .agent_events import RuntimeInfoObservation
    from .threads import Thread


class RuntimeInfo(DeclaredFamily, affix="RuntimeInfo"):
    """A runtime-info read is observed or explicitly unobserved."""

    @abstractmethod
    def context_reading(self) -> ContextReading: ...

    @abstractmethod
    def model_or(self, default: str) -> str: ...

    session_name: str | None
    context_used: PiCount
    context_size: PiCount


@dataclass(frozen=True, slots=True)
class UnobservedRuntimeInfo(RuntimeInfo):
    """No observation exists; do not invent a model or observation timestamp."""

    def context_reading(self) -> ContextReading:
        return UnmeasuredContext()

    session_name = None
    context_used = context_size = UnreportedCount()

    def model_or(self, default: str) -> str:
        return default


@dataclass(frozen=True, slots=True)
class AgentRuntimeInfo(RuntimeInfo):
    """Declares mutable runtime metadata for one registered thread."""

    thread: str
    model: str | None = None
    session_name: str | None = None
    context_used: PiCount = field(default_factory=UnreportedCount)
    context_size: PiCount = field(default_factory=UnreportedCount)
    timestamp: float = field(metadata={"wire_name": "ts", "wire_required": True}, kw_only=True)

    def __post_init__(self) -> None:
        FieldCodec.decode(float, self.timestamp)
        if not self.thread:
            raise RelationViolationError("Runtime-info thread cannot be empty.")

    def context_reading(self) -> ContextReading:
        return self.context_size.reading(self.context_used)

    def model_or(self, default: str) -> str:
        return self.model or default


@dataclass(frozen=True, slots=True)
class RuntimeInfoStore(RootFile, ThreadOwnedState, LockedStore[dict[str, AgentRuntimeInfo]]):
    """Latest observations, independently committed from the registry/log."""

    filename: ClassVar[str] = "runtime_info.json"
    json_indent = 2

    @property
    def record_type(self) -> type[dict[str, AgentRuntimeInfo]]:
        return dict[str, AgentRuntimeInfo]

    def empty(self) -> dict[str, AgentRuntimeInfo]:
        return {}

    def for_thread(self, thread: str) -> RuntimeInfo:
        return self.for_threads((thread,))[thread]

    def for_threads(self, names: Iterable[str]) -> dict[str, RuntimeInfo]:
        values = self.read()
        resolved = {}
        for name in names:
            try:
                resolved[name] = values[name]
            except KeyError:
                resolved[name] = UnobservedRuntimeInfo()
        return resolved

    def observe(self, thread: str, timestamp: float, observation: RuntimeInfoObservation) -> None:
        """Apply only this observation's fields in the original store transaction."""

        def apply(values: dict[str, AgentRuntimeInfo]) -> dict[str, AgentRuntimeInfo]:
            current = values.get(thread)
            if current is None:
                current = AgentRuntimeInfo(thread=thread, timestamp=timestamp)
            observed = observation.project_runtime_info(replace(current, timestamp=timestamp))
            return {**values, thread: observed}

        self.update(apply)

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        names = {thread.name for thread in threads}
        self.update(
            lambda values: (
                {name: value for name, value in values.items() if name not in names}
                if names & values.keys()
                else values
            )
        )

    def rename_thread(self, old_name: str, new_name: str) -> None:
        def rename(values: dict[str, AgentRuntimeInfo]) -> dict[str, AgentRuntimeInfo]:
            if old_name not in values or old_name == new_name:
                return values
            return {
                **{name: value for name, value in values.items() if name != old_name},
                new_name: replace(values[old_name], thread=new_name),
            }

        self.update(rename)
