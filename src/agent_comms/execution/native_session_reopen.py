"""Read-only saved identity selection; native startup owns strict history loading."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from agent_comms.execution.session_retention import NativeSessionRetention
from agent_comms.execution.turn_item import FreshNativeId
from agent_comms.nominal.field_codec import (
    FieldCodec,
    MalformedRecord,
    NullMember,
    PresentMember,
    WireName,
)

if TYPE_CHECKING:
    from agent_comms.execution.native_child import PiSessionChild
    from agent_comms.execution.native_runtime_input import NativeRuntimeInput


class NativeReopenError(ValueError):
    """Saved session cannot be safely reloaded; no provider input may start."""


class NativeSessionSelection:
    """The selected-id declaration owns the saved-session answer for an input."""

    def require_recorded_session(self) -> PiSessionId:
        raise MalformedRecord("Native reference lacks recorded session identity")

    def selected_session(self, row: NativeRuntimeInput) -> NativeSessionIdentity | NoSessionRetention:
        FieldCodec.decode(type(None), row.session_file)
        return NoSessionRetention()


class PiSessionId(FreshNativeId, NativeSessionSelection):
    """The Pi session manager's saved session identity."""

    def require_recorded_session(self) -> PiSessionId:
        return self

    def selected_session(self, row: NativeRuntimeInput) -> NativeSessionIdentity:
        return row.require_session_identity()


@dataclass(frozen=True)
class NoPiSessionId(NullMember, NativeSessionSelection):
    """The original native input has not selected a saved session."""


class SessionRetention(ABC):
    """Only an attested saved identity can return a child to its session owner."""

    @abstractmethod
    def retain[State](self, child: PiSessionChild, session: NativeSessionRetention[PiSessionChild, State, NativeSessionIdentity]) -> bool: ...

    @abstractmethod
    def require_identity(self) -> NativeSessionIdentity: ...

    @abstractmethod
    def allows_extension_input(self, inputs) -> bool: ...

    @abstractmethod
    def conflicts_with(self, observed: SessionRetention) -> bool:
        """Whether an observed identity contradicts the one this turn expected."""


class NoSessionRetention(NullMember, SessionRetention):
    def retain[State](self, child: PiSessionChild, session: NativeSessionRetention[PiSessionChild, State, NativeSessionIdentity]) -> bool:
        return False

    def require_identity(self) -> NativeSessionIdentity:
        raise ValueError("Native child has not attested an original session identity")

    def allows_extension_input(self, inputs) -> bool:
        return False

    def conflicts_with(self, observed: SessionRetention) -> bool:
        return False


@dataclass(frozen=True)
class NativeSessionIdentity(PresentMember, SessionRetention):
    """SessionManager's observed saved-session identity."""

    session_id: Annotated[PiSessionId, WireName("sessionId")]
    session_file: Annotated[str, WireName("sessionFile")]

    def require_identity(self) -> NativeSessionIdentity:
        return self

    def allows_extension_input(self, inputs) -> bool:
        return inputs.permits_interaction

    def conflicts_with(self, observed: SessionRetention) -> bool:
        return observed != self

    def __post_init__(self):
        if not self.session_id or not Path(self.session_file).is_absolute():
            raise NativeReopenError("Saved native session identity is incomplete")

    def retain[State](self, child: PiSessionChild, session: NativeSessionRetention[PiSessionChild, State, NativeSessionIdentity]) -> bool:
        return session.retain(child, self)

    @property
    def path(self) -> Path:
        return Path(self.session_file)

    def require_same_session(self, other: NativeSessionIdentity) -> None:
        if not self.same_session(other):
            raise NativeReopenError("Native saved source identity changed")

    def require_context(self, context) -> None:
        self.require_same_session(context.session_identity())

    def same_session(self, other: NativeSessionIdentity) -> bool:
        """A cutpoint may extend this identity without changing its meaning."""
        return self.session_id == other.session_id and self.session_file == other.session_file

    def require_session(self, canonical: str) -> None:
        if self.session_file != canonical:
            raise ValueError("Native identity differs from owner's canonical session")
