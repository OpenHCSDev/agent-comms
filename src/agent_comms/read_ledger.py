"""The sole human read authority, keyed by viewer and stable conversation.

Sparse message membership is intentional: a bounded or filtered display can
skip older messages even within one conversation. A maximum sequence cannot
represent that fact. Delivery cursors remain a separate executor contract.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .locked_store import LockedStore
from .private_path import FileIdentity, FileRevision
from .read_basis import Conversation, DisplayBasis, DisplayedConversation
from .sealed import Sealed
from .thread_identity import RecordedThreadIncarnation
from .thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery
    from .messages import Message
    from .registry_document import RegistrySnapshot
    from .registry_provenance import RegistryProvenance
    from .thread_identity import ThreadRole
    from .threads import Thread


@dataclass(frozen=True, slots=True)
class ReadDocument:
    messages: dict[str, tuple[int, ...]] = field(default_factory=dict)
    bus_identity: tuple[int, int] | None = None
    transcripts: dict[str, int] = field(default_factory=dict)
    notice: str | None = None

    def seen_sequences(
        self, viewer: str, snapshot: RegistryProvenance, source: FileIdentity | None
    ) -> frozenset[int]:
        """Project this document against the reader's actual source identity."""
        viewer = snapshot.canonical_name(viewer)
        thread = snapshot.threads[viewer]
        recorded = FileIdentity(*self.bus_identity) if self.bus_identity is not None else None
        if recorded != source:
            return frozenset()
        seen: set[int] = set()
        for key, sequences in self.messages.items():
            name, created, raw = json.loads(key)
            if snapshot.canonical_name(name) == viewer and created == thread.created_at:
                conversation = Conversation.from_wire(raw)
                if conversation.current(snapshot):
                    seen.update(sequences)
        return frozenset(seen)


class ReadLedger(Sealed, ThreadOwnedState, LockedStore[ReadDocument]):
    filename = "read_ledger.json"

    @property
    def record_type(self) -> type[ReadDocument]:
        return ReadDocument

    def empty(self) -> ReadDocument:
        return ReadDocument()

    @staticmethod
    def human(role: ThreadRole) -> bool:
        return not role.executable

    @staticmethod
    def bus_identity(path: Path) -> tuple[int, int] | None:
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino

    @staticmethod
    def conversation(message: Message, snapshot: RegistryProvenance) -> Conversation:
        from .channel_targets import BuiltinChannel, is_channel_target

        builtin = BuiltinChannel.lookup(message.target)
        if builtin is not None or is_channel_target(message.target):
            return Conversation(target=builtin.value if builtin is not None else message.target)
        names = sorted(
            {snapshot.canonical_name(name) for name in (message.sender, message.target)}
        )
        return Conversation(
            participants=tuple(snapshot.participant(name).incarnation for name in names)
        )

    @staticmethod
    def _key(viewer: str, created_at: float, conversation: Conversation) -> str:
        return json.dumps([viewer, created_at, conversation.to_wire()], separators=(",", ":"))

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
            raise ValueError("Display bus changed; refresh the displayed page.")
        if viewer != displayed.viewer:
            raise ValueError("Display belongs to a different viewer.")

        def advance(document: ReadDocument) -> ReadDocument:
            messages = (
                dict(document.messages) if document.bus_identity == displayed.bus_identity else {}
            )
            for item in displayed.conversations:
                key = self._key(viewer, displayed.viewer_created_at, item.conversation)
                messages[key] = tuple(sorted(set(messages.get(key, ())) | set(item.sequences)))
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

    def seen_sequences(
        self, viewer: str, snapshot: RegistryProvenance, *, document: ReadDocument | None = None
    ) -> frozenset[int]:
        document = self.read() if document is None else document
        revision = FileRevision.capture(self.path.with_name("bus.jsonl"))
        return document.seen_sequences(viewer, snapshot, revision.identity if revision else None)

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
        if message.seq in self.seen_sequences(thread.name, snapshot, document=document):
            return (thread.incarnation,)
        return ()

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        """Drop a deleted viewer's reads and every read of its deleted transcript.

        Other viewers' reads of conversations with the deleted thread remain:
        those messages stay on the bus.
        """
        viewers = {thread.incarnation for thread in threads}
        viewer_names = {thread.name for thread in threads}
        sources = {
            str(Path(thread.session_file).resolve()) for thread in threads if thread.session_file
        }

        def retained_message(key: str) -> bool:
            name, created, _ = json.loads(key)
            return RecordedThreadIncarnation(name, created) not in viewers

        def retained_transcript(key: str) -> bool:
            name, source, _ = json.loads(key)
            return source not in sources and name not in viewer_names

        def remove(document: ReadDocument) -> ReadDocument:
            messages = {k: v for k, v in document.messages.items() if retained_message(k)}
            transcripts = {k: v for k, v in document.transcripts.items() if retained_transcript(k)}
            if messages == document.messages and transcripts == document.transcripts:
                return document
            return replace(document, messages=messages, transcripts=transcripts)

        self.update(remove)

    @staticmethod
    def _transcript_key(viewer: str, source: str, inode: int) -> str:
        return json.dumps([viewer, str(Path(source).resolve()), inode])

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
