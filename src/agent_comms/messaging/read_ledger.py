from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import (
    Callable,
    Iterator,
    Sequence,
)
from dataclasses import (
    dataclass,
    field,
    replace,
)
from pathlib import Path

from agent_comms.coordination.errors import DMScopeChanged
from agent_comms.messaging.bus import Bus, BusReader
from agent_comms.messaging.bus_publication import Delivered
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.messages import Message
from agent_comms.messaging.read_basis import (
    AllConversation,
    ChannelConversation,
    Conversation,
    ConversationCursor,
    DisplayBasis,
    DisplayedConversation,
    DMConversation,
    MessageDisplayScope,
    ReadCursor,
)
from agent_comms.messaging.recorded_bus import NoRecordedBus, RecordedBus, RecordedBusIdentity
from agent_comms.messaging.transcript_read_mark import MarksTranscriptRead
from agent_comms.nominal.field_codec import FieldCodec, NullMember, SingleFieldValue, SubclassUnion
from agent_comms.nominal.sealed import Sealed
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.registry_provenance import RegistryProvenance
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.thread_owned_state import ThreadOwnedState
from agent_comms.registry.threads import Thread
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.storage.maintenance_barrier import CarriedDocument
from agent_comms.storage.private_path import ObservedPath
from agent_comms.storage.store_files import DependentStateTier, SingleFileStore


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
class PendingReadNotice(SingleFieldValue, ReadNotice):
    text: str

    def take(self, ledger: ReadLedger) -> ReadNotice:
        def clear(document: ReadDocument) -> DocumentChange[ReadDocument, ReadNotice]:
            if document.notice != self:
                return DocumentChange(document, NoReadNotice())
            return DocumentChange(replace(document, notice=NoReadNotice()), self)

        return ledger.update(clear)

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


class ReadLedger(MarksTranscriptRead, SingleFileStore, Sealed, ThreadOwnedState, CarriedDocument[ReadDocument], DependentStateTier):
    filename = "read_ledger.json"

    record_type = ReadDocument

    def empty(self) -> ReadDocument:
        return ReadDocument()

    def read_conversation(self, viewer: str, conversation: Conversation,
                          snapshot: RegistryProvenance) -> ReadDocument:
        owners = tuple(snapshot.threads[snapshot.canonical_name(viewer)].incarnation.recorded_names(snapshot))
        conversations = (AllConversation(), *conversation.recorded_keys(snapshot))
        return self._read_using(lambda data: FieldCodec.decode_selected(ReadDocument, data,
            {(ReadDocument, "messages"): owners, (ReadCursor, "conversations"): conversations},
            fields=("messages", "bus_identity")))

    @staticmethod
    def conversation(message: Message, snapshot: RegistryProvenance) -> Conversation:
        return CommandTarget.named(message.target).conversation_key(message.sender, snapshot, ChannelConversation, DMConversation)

    def capture(self, viewer, messages, snapshot, source: BusReader, *,
                        document: ReadDocument, conversation_snapshot=None) -> DisplayBasis:
        viewer = snapshot.canonical_name(viewer)
        provenance = conversation_snapshot or snapshot
        grouped: dict[Conversation, set[int]] = {}
        for message in messages:
            conversation = self.conversation(message, provenance)
            grouped.setdefault(conversation, set()).add(message.seq)
        with source.reading() as captured:
            return DisplayBasis(
                viewer, snapshot.threads[viewer].incarnation.wire_birth,
                tuple(DisplayedConversation(
                    conversation, sequences := tuple(sorted(members)),
                    self._proved_read_prefix(
                        captured, conversation.scope(provenance),
                        document.for_conversation(viewer, conversation, snapshot, captured),
                        sequences),
                ) for conversation, members in grouped.items()),
                RecordedBus(captured.identity),
            )

    @staticmethod
    def _proved_read_prefix(
        source: BusReader, scope: MessageDisplayScope, previous: ConversationCursor,
        sequences: tuple[int, ...],
    ) -> int:
        """Find the first unread scoped seq; never decode the intervening history."""
        through = max((*sequences, *previous.exceptions, previous.high_water))
        predicate, parameters = scope.index_predicate
        missing = source.scoped_page(
            f"({predicate}) AND seq NOT IN (SELECT value FROM json_each(?))",
            (*parameters, json.dumps((*sequences, *previous.exceptions))),
            lower=previous.high_water, upper=through + 1, limit=1)
        return missing[0].seq - 1 if missing else through

    def mark_displayed(self, viewer: str, displayed: DisplayBasis) -> None:
        if not displayed.bus_identity.matches(ObservedPath.at(Bus.path_in(self.path.parent))):
            raise DMScopeChanged("Display bus changed; refresh the displayed page.")
        if viewer != displayed.viewer:
            raise DMScopeChanged("Display belongs to a different viewer.")

        def advance(document: ReadDocument) -> DocumentChange[ReadDocument, ReadDocument]:
            messages = (
                dict(document.messages)
                if document.bus_identity.matches(displayed.bus_identity) else {}
            )
            messages[displayed.viewer_identity] = messages.get(
                displayed.viewer_identity, ReadCursor(),
            ).add(displayed.conversations)
            return DocumentChange.to(
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
        self, original: Delivered, recipient, snapshot: RegistrySnapshot, *, document=None
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
            return incarnation
        return None

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

        def remove(document: ReadDocument) -> DocumentChange[ReadDocument, ReadDocument]:
            messages = {k: v for k, v in document.messages.items() if k not in viewers}
            transcripts = {k: v for k, v in document.transcripts.items() if k[1] not in sources and k[0] not in viewer_names}
            if messages == document.messages and transcripts == document.transcripts:
                return DocumentChange.to(document)
            return DocumentChange.to(replace(document, messages=messages, transcripts=transcripts))

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

        def advance(document: ReadDocument) -> DocumentChange[ReadDocument, ReadDocument]:
            previous = document.transcripts.get(key, 0)
            if previous <= size and previous >= through:
                return DocumentChange.to(document)
            return DocumentChange.to(replace(document, transcripts={**document.transcripts, key: through}))

        self.update(advance)
