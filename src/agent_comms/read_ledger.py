"""The sole human read authority, keyed by viewer and stable conversation.

Sparse message membership is intentional: a bounded or filtered display can
skip older messages even within one conversation. A maximum sequence cannot
represent that fact. Delivery cursors remain a separate executor contract.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .locked_store import LockedStore
from .read_basis import Conversation, DisplayBasis, DisplayedConversation
from .sealed import Sealed
from .thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from .messages import Message
    from .registry_document import RegistrySnapshot
    from .thread_identity import ThreadRole


@dataclass(frozen=True, slots=True)
class ReadDocument:
    messages: dict[str, tuple[int, ...]] = field(default_factory=dict)
    bus_identity: tuple[int, int] | None = None
    transcripts: dict[str, int] = field(default_factory=dict)
    notice: str | None = None


class ReadLedger(Sealed, LockedStore[ReadDocument]):
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
    def conversation(message: Message, snapshot: RegistrySnapshot) -> Conversation:
        from .channel_targets import BuiltinChannel, is_channel_target

        builtin = BuiltinChannel.lookup(message.target)
        if builtin is not None or is_channel_target(message.target):
            return Conversation(target=builtin.value if builtin is not None else message.target)
        names = sorted(
            {snapshot.aliases.get(name, name) for name in (message.sender, message.target)}
        )
        return Conversation(
            participants=tuple(
                ThreadIncarnation(
                    name, snapshot.threads[name].created_at if name in snapshot.threads else -1.0
                )
                for name in names
            )
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
        conversation_snapshot: RegistrySnapshot | None = None,
    ) -> DisplayBasis:
        viewer = snapshot.aliases.get(viewer, viewer)
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

    def seen_sequences(
        self, viewer: str, snapshot: RegistrySnapshot, *, document: ReadDocument | None = None
    ) -> frozenset[int]:
        viewer = snapshot.aliases.get(viewer, viewer)
        thread = snapshot.threads[viewer]
        seen: set[int] = set()
        document = self.read() if document is None else document
        if document.bus_identity != self.bus_identity(self.path.with_name("bus.jsonl")):
            return frozenset()
        for key, sequences in document.messages.items():
            name, created, raw = json.loads(key)
            if snapshot.aliases.get(name, name) == viewer and created == thread.created_at:
                conversation = Conversation.from_wire(raw)
                if conversation.current(snapshot):
                    seen.update(sequences)
        return frozenset(seen)

    def displayed_recipient(self, message, recipient, snapshot, *, document=None):
        """Return recorded human display evidence for this exact frozen recipient.

        Processing, assignment acceptance and a reply cannot grant this fact.
        A reused name with a different creation identity cannot inherit it.
        """
        from .bus_publication import stable_thread_lookup

        for thread in snapshot.threads.values():
            if (
                self.human(thread.role)
                and stable_thread_lookup(thread.created_at) == recipient.recipient_lookup
                and message.seq in self.seen_sequences(thread.name, snapshot, document=document)
            ):
                return (thread.incarnation,)
        return ()

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
