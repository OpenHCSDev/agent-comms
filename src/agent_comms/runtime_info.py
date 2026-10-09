"""Runtime info: declaration and persistence owners."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar

from .errors import RelationViolationError
from .field_codec import FieldCodec
from .locked_store import LockedStore
from .thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from .threads import Thread


@dataclass(frozen=True, slots=True)
class AgentRuntimeInfo:
    """Declares mutable runtime metadata for one registered thread."""

    thread: str
    model: str | None = None
    session_name: str | None = None
    context_used: int | None = None
    context_size: int | None = None
    timestamp: float = field(metadata={"wire_name": "ts", "wire_required": True}, kw_only=True)

    def __post_init__(self) -> None:
        FieldCodec.decode(float, self.timestamp)
        if not self.thread:
            raise RelationViolationError("Runtime-info thread cannot be empty.")
        if self.context_used is not None and self.context_used < 0:
            raise ValueError("Context usage cannot be negative.")
        if self.context_size is not None and self.context_size < 0:
            raise ValueError("Context size cannot be negative.")

    @property
    def context_percent(self) -> float | None:
        if self.context_used is None or not self.context_size:
            return None
        return self.context_used / self.context_size * 100

    @property
    def context_label(self) -> str:
        if self.context_percent is None:
            return ""
        return f"{self.context_used:,}/{self.context_size:,} tokens ({self.context_percent:.1f}%)"


@dataclass(frozen=True, slots=True)
class RuntimeInfoStore(ThreadOwnedState, LockedStore[dict[str, AgentRuntimeInfo]]):
    """Latest observations, independently committed from the registry/log."""

    filename: ClassVar[str] = "runtime_info.json"
    json_indent = 2

    @property
    def record_type(self) -> type[dict[str, AgentRuntimeInfo]]:
        return dict[str, AgentRuntimeInfo]

    def empty(self) -> dict[str, AgentRuntimeInfo]:
        return {}

    def set(self, info: AgentRuntimeInfo) -> None:
        self.update(lambda values: {**values, info.thread: info})

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
