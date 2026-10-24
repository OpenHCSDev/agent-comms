"""Bus position and content identity are one reference; neither alone is proof."""

from __future__ import annotations

from .class_fact import ClassFact
from .projection_source import ProducedRow, ProjectionSource
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .field_codec import MessageId, NullMember, RecordMember, SingleFieldValue, SubclassUnion
from .page_traversal import LiveCursor, NewestCursor, PageCursor, PageTraversal

if TYPE_CHECKING:
    from .read_basis import ReadCursor
    from .read_ledger import ReadDocument, ReadLedger
    from .registry_document import RegistrySnapshot


@dataclass(frozen=True, slots=True)
class MessageReference(ProjectionSource):

    @ClassFact
    def writes(cls) -> tuple[type[ProducedRow], ...]:
        from .bus_tables import MessageIdentitySource, AnsweredSource
        return (MessageIdentitySource, AnsweredSource)

    seq: int
    message_id: MessageId


class InboxStart(SubclassUnion, affix="Start"):
    """Where an unread read begins: after a message already taken, or at the first unread."""

    @abstractmethod
    def references(self) -> tuple[MessageReference, ...]:
        """The references the read must load to check its starting point."""

    @abstractmethod
    def cursor(self) -> int:
        """The exclusive bus sequence the read starts after."""

    def traversal(self) -> PageTraversal:
        """Unread messages are taken in delivery order, oldest first after the start."""
        return LiveCursor(self.cursor()).newer()

    def read_basis(self, reads: ReadLedger, actor: str, snapshot: RegistrySnapshot
                   ) -> tuple[ReadDocument, ReadCursor]:
        document = reads.read()
        return document, reads.cursor(actor, snapshot, document=document)

    def advanced(self, reference: MessageReference) -> AfterMessageStart:
        return AfterMessageStart(reference)


@dataclass(frozen=True, slots=True)
class AfterMessageStart(SingleFieldValue, InboxStart):
    reference: MessageReference

    def references(self) -> tuple[MessageReference, ...]:
        return (self.reference,)

    def cursor(self) -> int:
        return self.reference.seq


class StreamBeginning(InboxStart, declared_name=None):
    """Before the first message, there is no reference and the sequence is zero."""

    def references(self) -> tuple[MessageReference, ...]:
        return ()

    def cursor(self) -> int:
        return 0


class BridgePosition(InboxStart, declared_name=None):
    """The binding and journal own delivery position; human reads do not."""

    def advanced(self, reference: MessageReference) -> BridgeStart:
        return BridgeStart(reference)

    def read_basis(self, reads: ReadLedger, actor: str, snapshot: RegistrySnapshot
                   ) -> tuple[ReadDocument, ReadCursor]:
        document = reads.empty()
        return document, reads.cursor(actor, snapshot, document=document)


@dataclass(frozen=True, slots=True)
class BridgeStart(RecordMember, AfterMessageStart, BridgePosition):
    """A bridge position backed by a real, validated bus reference."""


@dataclass(frozen=True, slots=True)
class BridgeBeginning(RecordMember, StreamBeginning, BridgePosition):
    """An empty bus starts the bridge stream before its first message."""

@dataclass(frozen=True, slots=True)
class FirstUnreadStart(NullMember, StreamBeginning):
    """The first unread message is selected by the user's read ledger."""


@dataclass(frozen=True, slots=True)
class NewestUnreadStart(RecordMember, InboxStart):
    """A glance at the inbox: the newest unread page, paging older from there."""

    before: PageCursor = NewestCursor()

    def references(self) -> tuple[MessageReference, ...]:
        return ()

    def cursor(self) -> int:
        return 0

    def traversal(self) -> PageTraversal:
        return self.before.older()
