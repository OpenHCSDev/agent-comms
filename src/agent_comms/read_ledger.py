"""Human read authority: typed conversation identities and compact exact progress."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .locked_store import LockedStore
from .private_path import FileIdentity, FileRevision
from .read_basis import Conversation, DisplayBasis, DisplayedConversation
from .sealed import Sealed
from .thread_identity import ThreadIncarnation
from .thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from .messages import Message
    from .registry_provenance import RegistryProvenance
    from .thread_identity import ThreadRole
    from .threads import Thread


@dataclass(frozen=True, slots=True)
class ConversationKey:
    viewer: ThreadIncarnation
    conversation: Conversation

    def resolved(self, snapshot):
        return type(self)(self.viewer.resolved(snapshot), self.conversation.resolved(snapshot))


@dataclass(frozen=True, slots=True)
class TranscriptKey:
    viewer: str
    source: str
    inode: int

    @classmethod
    def capture(cls, viewer, source, inode):
        return cls(viewer, str(Path(source).resolve()), inode)


@dataclass(frozen=True, slots=True)
class ReadProgress:
    """The proven conversation prefix plus exact paints beyond its first gap."""
    read_through: int = 0
    sparse: frozenset[int] = frozenset()

    def __post_init__(self):
        if self.read_through < 0 or any(n <= self.read_through for n in self.sparse):
            raise ValueError('Read progress must keep only positive sequences above its prefix')

    def contains(self, sequence: int) -> bool:
        return 0 < sequence <= self.read_through or sequence in self.sparse

    def merge(self, other):
        through = max(self.read_through, other.read_through)
        return type(self)(through, frozenset(n for n in self.sparse | other.sparse if n > through))

    def mark(self, sequences, prefix):
        marked = self.merge(type(self)(sparse=frozenset(n for n in sequences if n > 0)))
        through = prefix(marked)
        if through is None:
            return marked  # An unavailable derived index cannot grant a read prefix.
        return type(self)(through, frozenset(n for n in marked.sparse if n > through))


@dataclass(frozen=True, slots=True)
class ReadMask:
    """One viewer's original, namespace-resolved read membership; never a history set."""
    conversations: dict[Conversation, ReadProgress] = field(default_factory=dict)
    incarnations: dict[str, ThreadIncarnation] = field(default_factory=dict)

    @classmethod
    def capture(cls, conversations, snapshot):
        incarnations = {
            identity.name: identity
            for conversation in conversations
            for participant in conversation.participants
            for identity in participant.recorded_names(snapshot)
        }
        return cls(conversations, incarnations)

    def incarnation(self, name):
        return self.incarnations.get(name, ThreadIncarnation.deleted(name))

    def conversation(self, sender, target):
        return Conversation.from_names(sender, target, self.incarnation)

    def progress(self, sender, target):
        return self.conversations.get(self.conversation(sender, target), ReadProgress())

    def contains(self, message) -> bool:
        return self.progress(message.sender, message.target).contains(message.seq)

    def contains_route(self, sender, target, sequence):
        return self.progress(sender, target).contains(sequence)

    def changed_ranges(self, previous):
        return tuple((min(before.read_through, after.read_through),
                      max(before.read_through, after.read_through))
                     for conversation in self.conversations.keys() | previous.conversations.keys()
                     for before in (previous.conversations.get(conversation, ReadProgress()),)
                     for after in (self.conversations.get(conversation, ReadProgress()),)
                     if before.read_through != after.read_through)

    @property
    def sparse(self):
        return frozenset(n for state in self.conversations.values() for n in state.sparse)


@dataclass(frozen=True, slots=True)
class ReadDocument:
    messages: dict[ConversationKey, ReadProgress] = field(default_factory=dict)
    bus_identity: tuple[int, int] | None = None
    transcripts: dict[TranscriptKey, int] = field(default_factory=dict)
    notice: str | None = None

    def normalized(self, snapshot):
        messages = {}
        for key, state in self.messages.items():
            key = key.resolved(snapshot)
            messages[key] = messages.get(key, ReadProgress()).merge(state)
        return replace(self, messages=messages)

    def memberships(self, snapshot, source: FileIdentity | None):
        recorded = FileIdentity(*self.bus_identity) if self.bus_identity is not None else None
        if recorded != source:
            return {}
        viewers = {}
        for key, state in self.messages.items():
            if not key.viewer.current(snapshot) or not key.conversation.current(snapshot):
                continue
            key = key.resolved(snapshot)
            conversations = viewers.setdefault(key.viewer.name, {})
            conversations[key.conversation] = conversations.get(key.conversation, ReadProgress()).merge(state)
        return {viewer: ReadMask.capture(conversations, snapshot)
                for viewer, conversations in viewers.items()}


class ReadLedger(Sealed, ThreadOwnedState, LockedStore[ReadDocument]):
    filename = 'read_ledger.json'

    @property
    def record_type(self):
        return ReadDocument

    def empty(self):
        return ReadDocument()

    @staticmethod
    def human(role: ThreadRole) -> bool:
        return not role.executable

    @staticmethod
    def bus_identity(path: Path):
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino

    @staticmethod
    def conversation(message, snapshot):
        return Conversation.from_names(message.sender, message.target,
                                       lambda name: snapshot.participant(name).incarnation)

    def capture(self, viewer, messages: Iterable[Message], snapshot, bus_path, *, conversation_snapshot=None):
        viewer = snapshot.canonical_name(viewer)
        grouped = {}
        for message in messages:
            conversation = self.conversation(message, conversation_snapshot or snapshot)
            grouped.setdefault(conversation, []).append(message.seq)
        return DisplayBasis(viewer, snapshot.threads[viewer].created_at,
                            tuple(DisplayedConversation(c, tuple(sorted(set(ns)))) for c, ns in grouped.items()),
                            self.bus_identity(bus_path))

    def mark_displayed(self, viewer, displayed, *, snapshot):
        bus_path = self.path.with_name('bus.jsonl')
        if displayed.bus_identity != self.bus_identity(bus_path):
            raise ValueError('Display bus changed; refresh the displayed page.')
        if viewer != displayed.viewer:
            raise ValueError('Display belongs to a different viewer.')
        from .bus_route_counts import BusRouteCounts, OpenedRouteCounts
        from .coordination_errors import CoordinationReadUnavailable
        from .wire_log import WireLog

        def advance(document, prefix):
            messages = (dict(document.normalized(snapshot).messages)
                        if document.bus_identity == displayed.bus_identity else {})
            for item in displayed.conversations:
                conversation = item.conversation.resolved(snapshot)
                key = ConversationKey(displayed.viewer_identity.resolved(snapshot), conversation)
                state = messages.get(key, ReadProgress())
                messages[key] = state.mark(item.sequences, lambda state: prefix(conversation, state))
            return (replace(document, messages=messages, bus_identity=displayed.bus_identity)
                    if messages != document.messages or document.bus_identity != displayed.bus_identity
                    else document)

        log = WireLog(bus_path)
        try:
            with log._opened_wire_snapshot(need_sequence=False) as source:
                prepared = OpenedRouteCounts(bus_path, source, snapshot, {})
                prepared.sync(source.stream, source.revision, prepared._decode, blocking=False)
            with BusRouteCounts(bus_path) as index:
                self.update(lambda document: advance(document, lambda conversation, state:
                    index.read_prefix(conversation, state, snapshot, displayed.bus_identity)))
        except CoordinationReadUnavailable:
            # Preserve exact paint even when compaction's disposable index is busy.
            self.update(lambda document: advance(document, lambda conversation, state: None))

    def take_notice(self, shown):
        taken = None
        def take(document):
            nonlocal taken
            if shown is None or document.notice != shown:
                return document
            taken = shown
            return replace(document, notice=None)
        self.update(take)
        return taken

    def observations(self, snapshot, *, document=None, source=None):
        document = self.read() if document is None else document
        if source is None:
            revision = FileRevision.capture(self.path.with_name('bus.jsonl'))
            source = revision.identity if revision else None
        return document.memberships(snapshot, source)

    def membership(self, viewer, snapshot, *, document=None):
        return self.observations(snapshot, document=document).get(snapshot.canonical_name(viewer), ReadMask())

    def displayed_recipient(self, message, recipient, snapshot, *, readers):
        name = snapshot.canonical_name(recipient.canonical_thread)
        thread = snapshot.threads.get(name)
        if thread is None or not self.human(thread.role):
            return ()
        return self._displayed_recipient(message, recipient, snapshot, readers)

    def historical_displayed_recipient(self, message, recipient, snapshot, *, readers):
        return self._displayed_recipient(message, recipient, snapshot, readers)

    @staticmethod
    def _displayed_recipient(message, recipient, snapshot, readers):
        from .bus_publication import stable_thread_lookup
        name = snapshot.canonical_name(recipient.canonical_thread)
        thread = snapshot.threads.get(name)
        if (thread is not None and stable_thread_lookup(thread.created_at) == recipient.recipient_lookup
                and readers.get(name, ReadMask()).contains(message)):
            return (thread.incarnation,)
        return ()

    def remove_threads(self, threads: Sequence[Thread]):
        viewers = {thread.incarnation for thread in threads}
        names = {thread.name for thread in threads}
        sources = {str(Path(thread.session_file).resolve()) for thread in threads if thread.session_file}
        def remove(document):
            messages = {k: v for k, v in document.messages.items() if k.viewer not in viewers}
            transcripts = {k: v for k, v in document.transcripts.items() if k.source not in sources and k.viewer not in names}
            return (document if messages == document.messages and transcripts == document.transcripts
                    else replace(document, messages=messages, transcripts=transcripts))
        self.update(remove)

    def transcript_seen(self, viewer, source, inode, *, document=None):
        document = self.read() if document is None else document
        return document.transcripts.get(TranscriptKey.capture(viewer, source, inode), 0)

    def mark_transcript(self, viewer, source, inode, through, size):
        key = TranscriptKey.capture(viewer, source, inode)
        def advance(document):
            previous = document.transcripts.get(key, 0)
            return (document if previous <= size and previous >= through else
                    replace(document, transcripts={**document.transcripts, key: through}))
        self.update(advance)
