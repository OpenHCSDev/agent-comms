"""Read-only saved identity selection; native startup owns strict history loading."""

from __future__ import annotations

from .native_failures import NativePiUnavailable

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, TYPE_CHECKING
from .field_codec import FieldCodec, MalformedRecord, NullMember, PresentMember, boundary_decoder, WireName
from abc import ABC, abstractmethod
from .private_path import FileRevision
from .turn_item import FreshNativeId

if TYPE_CHECKING:
    from .native_child import PiSessionChild
    from .native_runtime_input import NativeRuntimeInput
    from .native_entries import NativeEvidenceRead
    from agent_comms.native_pi import NativeContextProof
    from .pi_native_backend import PersistentPiSession


class NativeReopenError(ValueError):
    """Saved session cannot be safely reloaded; no provider input may start."""


class NativeSessionSelection:
    """The selected-id declaration owns the saved-session answer for an input."""

    def selected_session(self, row: NativeRuntimeInput) -> NativeSessionIdentity | NoSessionRetention:
        FieldCodec.decode(type(None), row.session_file)
        return NoSessionRetention()


class PiSessionId(FreshNativeId, NativeSessionSelection):
    """The Pi session manager's saved session identity."""

    def selected_session(self, row: NativeRuntimeInput) -> NativeSessionIdentity:
        return row.require_session_identity()


@dataclass(frozen=True)
class NoPiSessionId(NullMember, NativeSessionSelection):
    """The original native input has not selected a saved session."""


class SessionRetention(ABC):
    """Only an attested saved identity can return a child to its session owner."""

    @classmethod
    @boundary_decoder
    def from_optional(cls, identity: NativeSessionIdentity | None) -> SessionRetention:
        return NoSessionRetention() if identity is None else identity

    @abstractmethod
    def retain(self, child: PiSessionChild, session: PersistentPiSession) -> bool: ...


class NoSessionRetention(NullMember, SessionRetention):
    def retain(self, child: PiSessionChild, session: PersistentPiSession) -> bool:
        return False


@dataclass(frozen=True)
class NativeSessionIdentity(PresentMember, SessionRetention):
    """SessionManager's observed saved-session identity."""

    session_id: Annotated[PiSessionId, WireName("sessionId")]
    session_file: Annotated[str, WireName("sessionFile")]

    def __post_init__(self):
        if not self.session_id or not Path(self.session_file).is_absolute():
            raise NativeReopenError("Saved native session identity is incomplete")

    def retain(self, child: PiSessionChild, session: PersistentPiSession) -> bool:
        return session.retain(child, self)

    @property
    def path(self) -> Path:
        return Path(self.session_file)

    def require_same_session(self, other: NativeSessionIdentity) -> None:
        if not self.same_session(other):
            raise NativeReopenError("Native saved source identity changed")

    @staticmethod
    def from_context(context: NativeContextProof) -> NativeSessionIdentity:
        return NativeSessionIdentity(context.session_id, str(context.session_file))

    def require_context(self, context) -> None:
        self.require_same_session(self.from_context(context))

    def same_session(self, other: NativeSessionIdentity) -> bool:
        """A cutpoint may extend this identity without changing its meaning."""
        return self.session_id == other.session_id and self.session_file == other.session_file

    def require_session(self, canonical: str) -> None:
        if self.session_file != canonical:
            raise ValueError("Native identity differs from owner's canonical session")

    def covers(self, evidence: NativeEvidenceRead, revision: FileRevision,
               *other_revisions: FileRevision) -> bool:
        """Corroborate this session and its recorded positions in a held source.

        Revisions are the original prepared/returned observations, not freshly
        reconstructed positions. Later appends are allowed; original bytes and
        ancestry remain under the acquired evidence reader's validation. This
        relation grants neither enrollment nor permission to replay an input.
        """
        source = evidence.source
        return (
            self.path == source.path
            and self.session_id == evidence.entries[0].require_entry_id()
            and all(
                source.identity == revision.identity and source.size >= revision.size
                for revision in (revision, *other_revisions)
            )
        )

    @classmethod
    def from_evidence(cls, evidence: NativeEvidenceRead) -> NativeSessionIdentity:
        """Derive selected identity from its recipient's already verified source."""
        file = evidence.source.path
        try:
            session_id = evidence.header().require_reopen()
        except (MalformedRecord, NativePiUnavailable) as error:
            raise NativeReopenError("Saved native session identity cannot be read") from error
        return cls(session_id, str(file))

    @classmethod
    def locate(cls, session_file: str) -> NativeSessionIdentity:
        """One-shot callers own a bounded scope; selected callers borrow theirs."""
        from .native_entries import NativeEvidenceScope

        try:
            with NativeEvidenceScope() as scope:
                return cls.from_evidence(scope.for_source(Path(session_file).absolute()))
        except NativePiUnavailable as error:
            raise NativeReopenError("Saved native session source cannot be opened") from error
