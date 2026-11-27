"""Context provenance coordinates and digests, decoupled from turn-context assembly."""

from __future__ import annotations

import hashlib
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from agent_comms.errors import RelationViolationError
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.declared_family import DeclaredFamily, DeclaredFirstMembers
from agent_comms.nominal.field_codec import TurnId
from agent_comms.registry.thread_identity import ThreadIncarnation


@dataclass(frozen=True)
class Provenance(
    DeclaredFirstMembers, DeclaredFamily, affix="Provenance",
    discovery_package="agent_comms.cli_bridges.codex_records",
):
    """A source coordinate, not permission to execute or replay it."""

    @abstractmethod
    def public_description(self) -> str: ...

    def public_text(self, comms) -> str:
        """Read an authenticated source, never reconstruct request wording.

        The inspection owner checks membership before calling this method.
        Coordinates without original text evidence remain unavailable.
        """
        raise ValueError(f"Original text is not retained by {self.public_description()}")

    def preview_observations(self) -> tuple[PreviewProvenance, ...]:
        return ()

    def native_identities(self) -> tuple[NativeSessionIdentity, ...]:
        return ()

    def journal_entries(self, identity: NativeSessionIdentity) -> tuple[str, ...]:
        return ()

    def turn_recording(self) -> tuple[Provenance, ...]:
        return (self,)

    def entry_membership(self) -> tuple[Provenance, ...]:
        return ()

    def bus_references(self) -> tuple[MessageReference, ...]:
        """Only a wire coordinate names a committed bus message."""
        return ()

    def require_human_input(self):
        raise RelationViolationError("Source is not an original human input")


@dataclass(frozen=True, kw_only=True)
class Sha256Digest:
    """The SHA-256 of a value's rendered bytes."""

    sha256: str


@dataclass(frozen=True, kw_only=True)
class FileProvenance(Sha256Digest, Provenance):
    """Authenticated file bytes whose public text view is deliberately bounded."""

    max_public_text_bytes: ClassVar[int] = 8 * 1024 * 1024
    path: str

    def public_description(self) -> str:
        return f"File {self.path} · SHA256 {self.sha256}"

    def require_digest(self, observed: str) -> None:
        if observed != self.sha256:
            raise ValueError("Original file bytes are unavailable: the source has changed")

    def verify(self) -> None:
        """Verify arbitrary file bytes without materializing them in memory."""
        with Path(self.path).open("rb") as stream:
            self.require_digest(hashlib.file_digest(stream, "sha256").hexdigest())

    def public_text(self, comms) -> str:
        with Path(self.path).open("rb") as stream:
            raw = stream.read(self.max_public_text_bytes + 1)
        if len(raw) > self.max_public_text_bytes:
            raise ValueError(
                f"Original file is too large to be public text: {self.path!r} exceeds "
                f"{self.max_public_text_bytes} bytes"
            )
        self.require_digest(hashlib.sha256(raw).hexdigest())
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"Original file is not UTF-8 text: {self.path!r}") from error


class HistoricalInstructionProvenance(Provenance, declared_name=None):
    """A retained instruction from a source session, never current execution authority."""

    @abstractmethod
    def instruction_position(self) -> tuple[int, int]:
        """Its original record and instruction ordering coordinates."""
        ...


@dataclass(frozen=True)
class OwnerProvenance(Provenance):
    owner: ThreadIncarnation
    revision: str

    def public_description(self) -> str:
        return f"Owner {self.owner.name} · born {self.owner.created_at} · revision {self.revision}"


@dataclass(frozen=True)
class TurnProvenance(Provenance):
    """The owner's recorded turn itself, never a digest standing in for it."""

    owner: ThreadIncarnation
    turn: TurnId

    def public_description(self) -> str:
        return f"Owner {self.owner.name} · born {self.owner.created_at} · turn {self.turn}"


@dataclass(frozen=True)
class WireProvenance(Provenance):
    source: MessageReference

    def public_description(self) -> str:
        return f"Wire message {self.source.seq} · {self.source.message_id}"

    def public_text(self, comms) -> str:
        (original,) = comms.bus.bus.messages_for_references((self.source,))
        return original.body

    def bus_references(self) -> tuple[MessageReference, ...]:
        return (self.source,)


@dataclass(frozen=True)
class JournalProvenance(Provenance):
    path: str
    entries: tuple[str, ...]

    def public_description(self) -> str:
        name = Path(self.path).name or self.path
        count = len(self.entries)
        if count <= 3:
            return f"Native journal {name} · entries {', '.join(self.entries)}"
        return f"Native journal {name} · {count} entries ({self.entries[0][:8]}…{self.entries[-1][:8]})"

    def journal_entries(self, identity: NativeSessionIdentity) -> tuple[str, ...]:
        identity.require_session(self.path)
        return self.entries

    def turn_recording(self) -> tuple[Provenance, ...]:
        return ()

    def entry_membership(self) -> tuple[Provenance, ...]:
        return (self,)


@dataclass(frozen=True, kw_only=True)
class NativeContextSource:
    """A provenance naming the one native session whose context digest it observed."""

    identity: NativeSessionIdentity
    context_digest: str

    def native_identities(self) -> tuple[NativeSessionIdentity, ...]:
        """SDK identity for checking a read, never permission to start input."""
        return (self.identity,)


@dataclass(frozen=True, kw_only=True)
class NativeProvenance(NativeContextSource, Provenance):
    request_generation: int

    def public_description(self) -> str:
        return (f"Native request {self.identity.session_id} · generation {self.request_generation} "
                f"· context {self.context_digest}")


@dataclass(frozen=True, kw_only=True)
class PreviewProvenance(NativeContextSource, Provenance):
    def public_description(self) -> str:
        return f"Current native preview {self.identity.session_id} · context {self.context_digest}"

    def preview_observations(self) -> tuple[PreviewProvenance, ...]:
        return (self,)


@dataclass(frozen=True, kw_only=True)
class ResourceProvenance(Sha256Digest, Provenance):
    path: str
    representation: str

    def public_description(self) -> str:
        return f"Resource {self.path} · {self.representation} · SHA256 {self.sha256}"


@dataclass(frozen=True)
class UnattributedProvenance(Provenance):
    """Captured wording with no original assembly attribution."""

    def public_description(self) -> str:
        return "Unattributed captured text · original source was not observed"
