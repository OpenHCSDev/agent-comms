"""The sole human read authority, keyed by viewer and stable conversation.

Sparse paint remains exact: the cursor compresses only a proved contiguous
prefix and retains out-of-order reads as exceptions. Conversation and source
identity still determine whether those reads apply.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .channel_targets import CommandTarget
from .coordination_errors import DMScopeChanged
from .locked_store import LockedStore, RootFile
from .private_path import FileIdentity, FileRevision, ObservedPath
from .read_basis import Conversation, DisplayBasis, DisplayedConversation, ReadCursor
from .sealed import Sealed
from .thread_identity import RecordedThreadIncarnation
from .thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery
    from .messages import Message
    from .registry_document import RegistrySnapshot
    from .registry_provenance import RegistryProvenance
    from .threads import Thread


@dataclass(frozen=True, slots=True)
class ReadDocument:
    messages: dict[RecordedThreadIncarnation, ReadCursor] = field(default_factory=dict)
    bus_identity: tuple[int, int] | None = None
    transcripts: dict[tuple[str, str, int], int] = field(default_factory=dict)
    notice: str | None = None

    def cursor(
        self, viewer: str, snapshot: RegistryProvenance, source
    ) -> ReadCursor:
        """Project this document against the reader's actual source identity."""
        viewer = snapshot.canonical_name(viewer)
        thread = snapshot.threads[viewer]
        recorded = FileIdentity(*self.bus_identity) if self.bus_identity is not None else None
        if recorded is None or not source.same_file(recorded):
            return ReadCursor()
        return ReadCursor.union(
            self.messages.get(owner, ReadCursor()).current(snapshot)
            for owner in thread.incarnation.recorded_names(snapshot)
        )


class ReadLedger(RootFile, Sealed, ThreadOwnedState, LockedStore[ReadDocument]):
    lock_rank = 4
    filename = "read_ledger.json"

    @property
    def record_type(self) -> type[ReadDocument]:
        return ReadDocument

    def empty(self) -> ReadDocument:
        return ReadDocument()

    @staticmethod
    def bus_identity(path: Path) -> tuple[int, int] | None:
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino

    @staticmethod
    def conversation(message: Message, snapshot: RegistryProvenance) -> Conversation:
        return CommandTarget.named(message.target).conversation_key(message.sender, snapshot, Conversation)

    def capture(
        self,
        viewer: str,
        messages: Iterable[Message],
        snapshot: RegistrySnapshot,
        bus_path: Path,
        *,
        conversation_snapshot: RegistryProvenance | None = None,
    ) -> DisplayBasis:
        viewer = snapshot.canonical_name(viewer)
        grouped: dict[Conversation, list[int]] = {}
        for message in messages:
            conversation = self.conversation(message, conversation_snapshot or snapshot)
            grouped.setdefault(conversation, []).append(message.seq)
        return DisplayBasis(
            viewer,
            snapshot.threads[viewer].created_at,
            tuple(
                DisplayedConversation(conversation, tuple(sorted(set(sequences))))
                for conversation, sequences in grouped.items()
            ),
            self.bus_identity(bus_path),
        )

    def mark_displayed(self, viewer: str, displayed: DisplayBasis) -> None:
        if displayed.bus_identity != self.bus_identity(self.path.with_name("bus.jsonl")):
            raise DMScopeChanged("Display bus changed; refresh the displayed page.")
        if viewer != displayed.viewer:
            raise DMScopeChanged("Display belongs to a different viewer.")

        def advance(document: ReadDocument) -> ReadDocument:
            messages = (
                dict(document.messages) if document.bus_identity == displayed.bus_identity else {}
            )
            messages[displayed.viewer_identity] = messages.get(
                displayed.viewer_identity, ReadCursor(),
            ).add(displayed.conversations)
            return (
                replace(document, messages=messages, bus_identity=displayed.bus_identity)
                if messages != document.messages or document.bus_identity != displayed.bus_identity
                else document
            )

        self.update(advance)

    def take_notice(self, shown: str | None) -> str | None:
        """Clear the stored notice once it has been shown; return it when this call cleared it."""
        taken = None

        def take(document: ReadDocument) -> ReadDocument:
            nonlocal taken
            if shown is None or document.notice != shown:
                return document
            taken = shown
            return replace(document, notice=None)

        self.update(take)
        return taken

    def cursor(
        self, viewer: str, snapshot: RegistryProvenance, *, document: ReadDocument | None = None
    ) -> ReadCursor:
        document = self.read() if document is None else document
        return document.cursor(viewer, snapshot, ObservedPath.at(self.path.with_name("bus.jsonl")))

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

    def _displayed_recipient(self, message, thread, snapshot, *, document):
        conversation = self.conversation(message, snapshot)
        if message.seq in self.cursor(thread.name, snapshot, document=document).for_conversation(conversation):
            return (thread.incarnation,)
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
