"""Lightweight transcript record and cut bases shared by native and external stores."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.storage.file_observation import FileIdentity, MissingFile
from agent_comms.storage.typed_table import TypedRow


class RecordsCut(DeclaredFamily, affix="Cut"):
    """How far a thread's own records reached when its page was read.

    Members supply ``offset``: the page position just after their last record.
    """

    offset: int

    def holds(self, current: RecordsCut, through: int) -> bool:
        """Whether ``current`` still contains the records this page read through."""
        return type(current) is type(self) and self.contains(current, through)

    def appended_by(self, current: RecordsCut) -> bool:
        """Whether ``current`` differs from this cut only by a forward suffix."""
        return self.holds(current, self.offset)

    def appends_external(self, prior: RecordsCut) -> bool:
        """Only an external cut with the same lower bound appends another external cut."""
        return False

    @abstractmethod
    def contains(self, current, through: int) -> bool: ...

    @abstractmethod
    def draft_current(self, current: RecordsCut) -> bool:
        """Whether this cut can still accompany the source's current draft."""

    def file_identity(self) -> FileIdentity | MissingFile:
        """The file whose bytes this cut's offsets index; records kept outside a file have none."""
        return MissingFile()

    def indexes(self, file: FileIdentity | MissingFile, offset: int) -> bool:
        """Whether an offset read from ``file`` still indexes this source's records. Records kept
        outside a file are never replaced underneath their offsets."""
        return True


class TranscriptRecord:
    """A source position; only native records can project a decoded native entry."""

    def project[Result](self, projection: Callable[..., Result]) -> Result | tuple[()]:
        """External records have events already, but no native entry for this callback."""
        return ()


@dataclass(frozen=True)
class PublishedReplyRevision(TypedRow):
    """Derived identity of this source's immutable published native relations."""

    through_seq: int
    inputs: int
