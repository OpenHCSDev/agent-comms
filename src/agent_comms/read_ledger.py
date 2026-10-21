"""The sole human read authority, keyed by viewer and stable conversation.

Sparse paint remains exact: the cursor compresses only a proved contiguous
prefix and retains out-of-order reads as exceptions. Conversation and source
identity still determine whether those reads apply.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .channel_targets import CommandTarget
from .coordination_errors import DMScopeChanged
from .field_codec import FieldCodec, NullMember, SubclassUnion
from .locked_store import DurableFile, LockedStore
from .private_path import ObservedPath
from .read_basis import (
    ChannelConversation,
    Conversation,
    ConversationCursor,
    DisplayBasis,
    DisplayedConversation,
    DMConversation,
    ReadCursor,
)
from .recorded_bus import NoRecordedBus, RecordedBusIdentity
from .sealed import Sealed
from .store_files import SharedLock
from .thread_identity import RecordedThreadIncarnation
from .thread_owned_state import ThreadOwnedState
from .bus import Bus

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery
    from .messages import Message
    from .registry_document import RegistrySnapshot
    from .registry_provenance import RegistryProvenance
    from .threads import Thread


class ReadNotice(SubclassUnion, affix="ReadNotice"):
    """A decoded notice owns consumption and whether it has anything to display."""

    @abstractmethod
    def take(self, ledger: ReadLedger) -> ReadNotice: ...

    @abstractmethod
    def notify_if_new(self, previous: ReadNotice, notify: Callable[[str], object]) -> None: ...


@dataclass(frozen=True)
class NoReadNotice(NullMember, ReadNotice):
    def take(self, ledger: ReadLedger) -> ReadNotice:
        return self

    def notify_if_new(self, previous: ReadNotice, notify: Callable[[str], object]) -> None:
        pass


@dataclass(frozen=True)
class PendingReadNotice(ReadNotice):
    text: str
    accepts_null = False

    @classmethod
    def from_wire(cls, data: object) -> PendingReadNotice:
        return cls(FieldCodec.decode(str, data))

    def to_wire(self) -> str:
        return self.text

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(str)

    def take(self, ledger: ReadLedger) -> ReadNotice:
        taken: ReadNotice = NoReadNotice()

        def clear(document: ReadDocument) -> ReadDocument:
            nonlocal taken
            if document.notice != self:
                return document
            taken = self
            return replace(document, notice=NoReadNotice())

        ledger.update(clear)
        return taken

    def notify_if_new(self, previous: ReadNotice, notify: Callable[[str], object]) -> None:
        if self != previous:
            notify(self.text)


@dataclass(frozen=True, slots=True)
class ReadDocument:
    messages: dict[RecordedThreadIncarnation, ReadCursor] = field(default_factory=dict)
    bus_identity: RecordedBusIdentity = field(default_factory=NoRecordedBus)
    transcripts: dict[tuple[str, str, int], int] = field(default_factory=dict)
    notice: ReadNotice = field(default_factory=NoReadNotice)

    def cursors(self, viewer: str, snapshot: RegistryProvenance,
                source: ObservedPath) -> Iterator[ReadCursor]:
        if self.bus_identity.matches(source):
            thread = snapshot.threads[snapshot.canonical_name(viewer)]
            for owner in thread.incarnation.recorded_names(snapshot):
                yield self.messages.get(owner, ReadCursor())

    def cursor(self, viewer: str, snapshot: RegistryProvenance,
               source: ObservedPath) -> ReadCursor:
        return ReadCursor.union(cursor.current(snapshot)
                                for cursor in self.cursors(viewer, snapshot, source))

    def for_conversation(self, viewer: str, conversation: Conversation,
                         snapshot: RegistryProvenance,
                         source: ObservedPath) -> ConversationCursor:
        keys = tuple(conversation.recorded_keys(snapshot))
        return ConversationCursor.union(
            cursor.for_conversation(key)
            for cursor in self.cursors(viewer, snapshot, source) for key in keys
        )


class ReadLedger(DurableFile, Sealed, ThreadOwnedState, LockedStore[ReadDocument]):
    filename = "read_ledger.json"

    record_type = ReadDocument

    def empty(self) -> ReadDocument:
        return ReadDocument()

    def read_conversation(self, viewer: str, conversation: Conversation,
                          snapshot: RegistryProvenance) -> ReadDocument:
        owners = tuple(snapshot.threads[snapshot.canonical_name(viewer)].incarnation.recorded_names(snapshot))
        conversations = tuple(conversation.recorded_keys(snapshot))
        with self.locked(mode=SharedLock()):
            return self._read_using(lambda data: FieldCodec.decode_selected(ReadDocument, data,
                {(ReadDocument, "messages"): owners, (ReadCursor, "conversations"): conversations},
                fields=("messages", "bus_identity")))

    @staticmethod
    def conversation(message: Message, snapshot: RegistryProvenance) -> Conversation:
        return CommandTarget.named(message.target).conversation_key(message.sender, snapshot, ChannelConversation, DMConversation)

    def capture(self, viewer, messages, snapshot, source: Bus, *,
                        document: ReadDocument, conversation_snapshot=None) -> DisplayBasis:
        viewer = snapshot.canonical_name(viewer)
        provenance = conversation_snapshot or snapshot
        grouped: dict[Conversation, set[int]] = {}
        for message in messages:
            conversation = self.conversation(message, provenance)
            grouped.setdefault(conversation, set()).add(message.seq)
        return DisplayBasis(
            viewer, snapshot.threads[viewer].created_at,
            tuple(DisplayedConversation(
                conversation, sequences := tuple(sorted(members)),
                0,
            ) for conversation, members in grouped.items()),
            RecordedBusIdentity.capture(source.path),
        )

    def mark_displayed(self, viewer: str, displayed: DisplayBasis) -> None:
        if not displayed.bus_identity.matches(ObservedPath.at(Bus.path_in(self.path.parent))):
            raise DMScopeChanged("Display bus changed; refresh the displayed page.")
        if viewer != displayed.viewer:
            raise DMScopeChanged("Display belongs to a different viewer.")

        def advance(document: ReadDocument) -> ReadDocument:
            messages = (
                dict(document.messages)
                if document.bus_identity.matches(displayed.bus_identity) else {}
            )
            messages[displayed.viewer_identity] = messages.get(
                displayed.viewer_identity, ReadCursor(),
            ).add(displayed.conversations)
            return (
                replace(document, messages=messages, bus_identity=displayed.bus_identity)
                if messages != document.messages
                or not document.bus_identity.matches(displayed.bus_identity)
                else document
            )

        self.update(advance)

    def cursor(
        self, viewer: str, snapshot: RegistryProvenance, *, document: ReadDocument | None = None
    ) -> ReadCursor:
        document = self.read() if document is None else document
        return document.cursor(viewer, snapshot, ObservedPath.at(Bus.path_in(self.path.parent)))

    def displayed_recipient(
        self, original: CommittedDelivery, recipient, snapshot: RegistrySnapshot, *, document=None
    ):
        """Return recorded human display evidence for this exact frozen recipient.

        Processing, assignment acceptance and a reply cannot grant this fact.
        A reused name with a different creation identity cannot inherit it.
        """
        return snapshot.recipient(recipient, original).read_effect(
            self, original.message, snapshot, document=document
        )

    def historical_displayed_recipient(
        self, message, recipient, snapshot: RegistryProvenance, *, document=None
    ):
        """Read this archive's human paint ledger, never an original CLI ACK.

        HistoryArchive does not copy the source's read ledger. Its new ledger
        is written by HistoricalDisplay.acknowledge after verifying the human
        viewer's incarnation. A same-name destination reader cannot attest a
        different frozen recipient or replace the recorded namespace.
        """
        # Archive selection binds both the original name and birth. A live
        # lookup index would conflate different names with the same birth.
        return snapshot.historical_recipient(recipient).read_effect(
            self, message, snapshot, document=document
        )

    def displayed_incarnation(self, message, incarnation, snapshot, *, document):
        conversation = self.conversation(message, snapshot)
        document = (self.read_conversation(incarnation.name, conversation, snapshot)
                    if document is None else document)
        if message.seq in document.for_conversation(
            incarnation.name, conversation, snapshot, ObservedPath.at(Bus.path_in(self.path.parent))
        ):
            return (incarnation,)
        return ()

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """Drop a deleted viewer's reads and every read of its deleted transcript.

        Other viewers' reads of conversations with the deleted thread remain:
        those messages stay on the bus.
        """
        viewers = {thread.incarnation for thread in threads}
        viewer_names = {thread.name for thread in threads}
        sources = set()
        for thread in threads:
            sources.update(thread.runtime.read_keys())

        def remove(document: ReadDocument) -> ReadDocument:
            messages = {k: v for k, v in document.messages.items() if k not in viewers}
            transcripts = {k: v for k, v in document.transcripts.items() if k[1] not in sources and k[0] not in viewer_names}
            if messages == document.messages and transcripts == document.transcripts:
                return document
            return replace(document, messages=messages, transcripts=transcripts)

        self.update(remove)

    @staticmethod
    def _transcript_key(viewer: str, source: str, inode: int) -> tuple[str, str, int]:
        return viewer, str(Path(source).resolve()), inode

    def transcript_seen(
        self, viewer: str, source: str, inode: int, *, document: ReadDocument | None = None
    ) -> int:
        document = self.read() if document is None else document
        return document.transcripts.get(self._transcript_key(viewer, source, inode), 0)

    def mark_transcript(
        self, viewer: str, source: str, inode: int, through: int, size: int
    ) -> None:
        key = self._transcript_key(viewer, source, inode)

        def advance(document: ReadDocument) -> ReadDocument:
            previous = document.transcripts.get(key, 0)
            if previous <= size and previous >= through:
                return document
            return replace(document, transcripts={**document.transcripts, key: through})

        self.update(advance)
