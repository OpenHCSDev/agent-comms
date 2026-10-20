"""Read-only saved identity selection; native startup owns strict history loading."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .field_codec import MalformedRecord, boundary_decoder
from .private_path import FileRevision

if TYPE_CHECKING:
    from .native_child import PiSessionChild
    from .native_entries import NativeEvidenceRead
    from .pi_native_backend import PersistentPiSession


class NativeReopenError(ValueError):
    """Saved session cannot be safely reloaded; no provider input may start."""


class SessionRetention(ABC):
    """Only an attested saved identity can return a child to its session owner."""

    @classmethod
    @boundary_decoder
    def from_optional(cls, identity: NativeSessionIdentity | None) -> SessionRetention:
        return NoSessionRetention() if identity is None else identity

    @abstractmethod
    def retain(self, child: PiSessionChild, session: PersistentPiSession) -> bool: ...


class NoSessionRetention(SessionRetention):
    def retain(self, child: PiSessionChild, session: PersistentPiSession) -> bool:
        return False


@dataclass(frozen=True)
class NativeSessionIdentity(SessionRetention):
    """SessionManager's observed saved-session identity."""

    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_file: str = field(metadata={"wire_name": "sessionFile"})

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

    def require_context(self, context) -> None:
        self.require_same_session(NativeSessionIdentity(context.session_id, str(context.session_file)))

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
        from .native_pi import NativePiUnavailable

        file = evidence.source.path
        try:
            header = evidence.header()
            header.require_reopen()
        except (MalformedRecord, NativePiUnavailable) as error:
            raise NativeReopenError("Saved native session identity cannot be read") from error
        return cls(header.require_entry_id(), str(file))

    @classmethod
    def locate(cls, session_file: str) -> NativeSessionIdentity:
        """One-shot callers own a bounded scope; selected callers borrow theirs."""
        from .native_entries import NativeEvidenceScope
        from .native_pi import NativePiUnavailable

        try:
            with NativeEvidenceScope() as scope:
                return cls.from_evidence(scope.for_source(Path(session_file).absolute()))
        except NativePiUnavailable as error:
            raise NativeReopenError("Saved native session source cannot be opened") from error
