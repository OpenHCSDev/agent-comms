"""Human read authority: typed conversation identities and compact exact progress."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .typed_table import Column, GeneratedColumn, In, TypedStore, TypedTable
from .field_codec import JsonShapeFamily, JsonShapeMember, projected
from .private_path import FileIdentity, FileRevision
from .read_basis import Conversation, ConversationIdentity, DisplayBasis, DisplayedConversation
from .sealed import Sealed
from .thread_identity import ThreadIncarnation
from .thread_owned_state import ThreadOwnedState

if TYPE_CHECKING:
    from .messages import Message
    from .registry_provenance import RegistryProvenance
    from .thread_identity import ThreadRole
    from .threads import Thread


ReadIdentity = tuple[float, ConversationIdentity]


@dataclass(frozen=True, slots=True)
class ConversationKey:
    viewer: ThreadIncarnation
    conversation: Conversation

    @projected(view='wire')
    def identity(self) -> ReadIdentity:
        return self.viewer.created_at, self.conversation.identity

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


class PrefixResult(DeclaredFamily, affix='Prefix'):
    def apply(self, marked: ReadProgress) -> ReadProgress:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ReadPrefix(PrefixResult):
    through: int

    def apply(self, marked: ReadProgress) -> ReadProgress:
        return ReadProgress(self.through, frozenset(n for n in marked.sparse if n > self.through))


@dataclass(frozen=True, slots=True)
class PrefixUnavailable(PrefixResult):
    def apply(self, marked: ReadProgress) -> ReadProgress:
        return marked


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

    def merge(self, other: ReadProgress) -> ReadProgress:
        through = max(self.read_through, other.read_through)
        return type(self)(through, frozenset(n for n in self.sparse | other.sparse if n > through))

    def mark(self, sequences: Iterable[int], prefix: Callable[[ReadProgress], PrefixResult]) -> ReadProgress:
        marked = self.merge(type(self)(sparse=frozenset(n for n in sequences if n > 0)))
        return prefix(marked).apply(marked)


@dataclass(frozen=True, slots=True)
class ReadMask:
    """One viewer's original, namespace-resolved read membership; never a history set."""
    conversations: dict[Conversation, ReadProgress] = field(default_factory=dict)
    incarnations: dict[str, ThreadIncarnation] = field(default_factory=dict)

    def incarnation(self, name: str) -> ThreadIncarnation:
        return self.incarnations[name]

    def conversation(self, sender, target):
        return Conversation.from_names(sender, target, self.incarnation)

    def progress(self, sender: str, target: str) -> ReadProgress:
        if not self.conversations:
            return ReadProgress()
        try:
            conversation = self.conversation(sender, target)
        except KeyError:
            return ReadProgress()  # Missing read identity is not a deletion claim.
        return self.conversations.get(conversation, ReadProgress())

    def contains(self, message: Message) -> bool:
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
class ReadObservations:
    """One prepared namespace cut; its source guards every consuming window."""
    source: ReadSource
    readers: dict[str, ReadMask]

    def for_source(self, source: ReadSource) -> dict[str, ReadMask]:
        return source.memberships(self)


class ReadNotice(DeclaredFamily, JsonShapeFamily, affix='ReadNotice'):
    def take(self, ledger: ReadLedger) -> ReadNotice:
        raise NotImplementedError

    def __bool__(self) -> bool:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class NoReadNotice(ReadNotice, JsonShapeMember):
    value: None = None

    def take(self, ledger: ReadLedger) -> ReadNotice:
        return self

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return ''


@dataclass(frozen=True, slots=True)
class ReadMarkerNotice(ReadNotice, JsonShapeMember):
    value: str

    def take(self, ledger: ReadLedger) -> ReadNotice:
        return ledger.take_notice(self)

    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ReadDocument:
    messages: dict[ConversationKey, ReadProgress]
    source: ReadSource
    transcripts: dict[TranscriptKey, int]
    notice: ReadNotice

    def transcript_seen(self, viewer, source, inode):
        return self.transcripts.get(TranscriptKey.capture(viewer, source, inode), 0)

    def normalized(self, snapshot):
        messages = {}
        for key, state in self.messages.items():
            key = key.resolved(snapshot)
            messages[key] = messages.get(key, ReadProgress()).merge(state)
        return replace(self, messages=messages)

    def observations(self, snapshot: RegistryProvenance) -> ReadObservations:
        viewers = {}
        for key, state in self.messages.items():
            if not key.viewer.current(snapshot) or not key.conversation.current(snapshot):
                continue
            key = key.resolved(snapshot)
            conversations = viewers.setdefault(key.viewer.name, {})
            conversations[key.conversation] = conversations.get(key.conversation, ReadProgress()).merge(state)
        names = {}
        for name, participant in snapshot.named_members():
            names.setdefault(participant.incarnation, []).append(name)
        return ReadObservations(self.source, {viewer: ReadMask(conversations, {
                    name: participant for conversation in conversations
                    for participant in conversation.participants for name in names[participant]
                }) for viewer, conversations in viewers.items()})


class ReadSource(DeclaredFamily, affix='ReadSource'):
    @classmethod
    def from_revision(cls, revision):
        return AbsentReadSource() if revision is None else PresentReadSource(revision.identity)

    def memberships(self, captured: ReadObservations) -> dict[str, ReadMask]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class PresentReadSource(ReadSource):
    identity: FileIdentity

    def memberships(self, captured: ReadObservations) -> dict[str, ReadMask]:
        return captured.readers if captured.source == self else {}


@dataclass(frozen=True, slots=True)
class AbsentReadSource(ReadSource):
    def memberships(self, captured: ReadObservations) -> dict[str, ReadMask]:
        return {}


class ReadTable:
    schema: ClassVar[int] = 1


@dataclass(frozen=True)
class ReadHeaderRow(ReadTable, TypedTable):
    source: ReadSource
    notice: ReadNotice = field(metadata={'sql': Column(nullable=True)})
    id: int = field(default=1, metadata={'sql': Column(primary_key=True, check='id=1')})

    @classmethod
    def current(cls, connection):
        current, = cls.select(connection, id=1)
        return current


@dataclass(frozen=True)
class ReadMessageRow(ReadTable, TypedTable):
    key: ConversationKey = field(metadata={'sql': Column(primary_key=True)})
    progress: ReadProgress
    viewer_birth: float = GeneratedColumn(expression="json_extract(key,'$.viewer.created_at')", index=True).field()
    identity: ReadIdentity = GeneratedColumn(expression="json_extract(key,'$.identity')", index=True).field()
    without_rowid = True

    @classmethod
    def selected(cls, connection, snapshot, sources):
        """Query exact viewer/conversation identities, including stable alias births."""
        identities = set()
        for source in sources:
            message = source.message
            conversation = Conversation.from_message(message, snapshot)
            names = {recipient.canonical_thread for recipient in source.audience.recipients}
            if conversation.participants:
                names.add(message.target)
            for name in names:
                viewer = snapshot.participant(name).incarnation
                identities.add(ConversationKey(viewer, conversation).identity)
        return {row.key: row.progress for row in cls.select(connection, identity=In(tuple(identities)))}


@dataclass(frozen=True)
class ReadTranscriptRow(ReadTable, TypedTable):
    key: TranscriptKey = field(metadata={'sql': Column(primary_key=True)})
    through: int
    viewer_name: str = GeneratedColumn(expression="json_extract(key,'$.viewer')", index=True).field()
    source_name: str = GeneratedColumn(expression="json_extract(key,'$.source')", index=True).field()
    without_rowid = True


class ReadViewers(DeclaredFamily, affix='Viewers'):
    def messages(self, connection):
        raise NotImplementedError

    def transcripts(self, connection):
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class SelectedViewers(ReadViewers):
    incarnations: tuple[ThreadIncarnation, ...]

    @classmethod
    def humans(cls, participants):
        return cls(tuple(p.incarnation for p in participants if ReadLedger.human(p.role)))

    def messages(self, connection):
        return ReadMessageRow.select(connection, viewer_birth=In(tuple(p.created_at for p in self.incarnations)))

    def transcripts(self, connection):
        return ReadTranscriptRow.select(connection, viewer_name=In(tuple(p.name for p in self.incarnations)))


@dataclass(frozen=True, slots=True)
class OneViewer(ReadViewers):
    incarnation: ThreadIncarnation

    def messages(self, connection):
        return ReadMessageRow.select(connection, viewer_birth=self.incarnation.created_at)

    def transcripts(self, connection):
        return ReadTranscriptRow.select(connection, viewer_name=self.incarnation.name)


@dataclass(frozen=True, slots=True)
class TranscriptViewer(ReadViewers):
    name: str

    def messages(self, connection):
        return ()

    def transcripts(self, connection):
        return ReadTranscriptRow.select(connection, viewer_name=self.name)


class ReadRevision(DeclaredFamily, affix='ReadRevision'):
    @classmethod
    def capture(cls, paths):
        files = {path.name: revision for path in paths
                 if (revision := FileRevision.capture(path)) is not None}
        return PresentReadRevision(files) if files else AbsentReadRevision()


@dataclass(frozen=True, slots=True)
class PresentReadRevision(ReadRevision):
    files: dict[str, FileRevision]


@dataclass(frozen=True, slots=True)
class AbsentReadRevision(ReadRevision):
    pass


@dataclass(frozen=True)
class ReadLedger(Sealed, ThreadOwnedState, TypedStore):
    """Canonical indexed read state, not a decoded-document cache."""
    root: Path
    filename: ClassVar[str] = f'read_ledger.v{ReadTable.schema}.sqlite3'
    rows: ClassVar = tuple(TypedTable.members_with(ReadTable))

    @classmethod
    def _document(cls, connection, viewers: ReadViewers):
        header = ReadHeaderRow.current(connection)
        messages = viewers.messages(connection)
        transcripts = viewers.transcripts(connection)
        return ReadDocument(
            {row.key: row.progress for row in messages}, header.source,
            {row.key: row.through for row in transcripts}, header.notice,
        )

    @contextmanager
    def snapshot(self):
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with super().snapshot() as connection:
            yield connection

    @contextmanager
    def reading(self, viewers: ReadViewers):
        with self.snapshot() as connection:
            yield self._document(connection, viewers)

    def read(self, viewers: ReadViewers):
        with self.reading(viewers) as document:
            return document

    def create(self, source: ReadSource, notice: ReadNotice):
        """Explicit fresh-root/cutover creation, never a reader's fallback."""
        if self.path.exists():
            raise FileExistsError(self.path)
        with self.writing() as connection:
            ReadHeaderRow(source, notice).insert(connection)

    def source_paths(self):
        return self.path, self.path.with_name(self.path.name + '-wal')

    def source_revision(self):
        return ReadRevision.capture(self.source_paths())

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

    def capture(self, viewer, messages: Iterable[Message], snapshot, bus_path, *, conversation_snapshot=None):
        viewer = snapshot.canonical_name(viewer)
        grouped = {}
        for message in messages:
            conversation = Conversation.from_message(message, conversation_snapshot or snapshot)
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
        if not displayed.conversations:
            return
        from .bus_route_counts import BusRouteCounts, OpenedRouteCounts
        from .coordination_errors import CoordinationReadUnavailable
        from .wire_log import WireLog

        def advance(prefix):
            with self.writing() as connection:
                header = ReadHeaderRow.current(connection)
                source = PresentReadSource(FileIdentity(*displayed.bus_identity))
                if header.source != source:
                    ReadMessageRow.delete(connection)
                    replace(header, source=source).upsert(connection)
                for item in displayed.conversations:
                    conversation = item.conversation.resolved(snapshot)
                    key = ConversationKey(displayed.viewer_identity.resolved(snapshot), conversation)
                    retained = ReadMessageRow.select(connection, identity=key.identity)
                    state = ReadProgress()
                    for row in retained:
                        state = state.merge(row.progress)
                    marked = state.mark(item.sequences, lambda value: prefix(conversation, value))
                    if len(retained) == 1 and retained[0].key == key and marked == state:
                        continue
                    for row in retained:
                        ReadMessageRow.delete(connection, key=row.key)
                    ReadMessageRow(key, marked).upsert(connection)

        log = WireLog(bus_path)
        try:
            with log._opened_wire_snapshot(need_sequence=False) as source:
                prepared = OpenedRouteCounts(bus_path, source, snapshot, {})
                prepared.sync(source.stream, source.revision, prepared._decode, blocking=False)
            with BusRouteCounts(bus_path) as index:
                advance(lambda conversation, state:
                    index.read_prefix(conversation, state, snapshot, displayed.bus_identity))
        except CoordinationReadUnavailable:
            # Preserve exact paint even when compaction's disposable index is busy.
            advance(lambda conversation, state: PrefixUnavailable())

    def take_notice(self, shown: ReadMarkerNotice) -> ReadNotice:
        with self.writing() as connection:
            header = ReadHeaderRow.current(connection)
            if header.notice != shown:
                return NoReadNotice()
            replace(header, notice=NoReadNotice()).upsert(connection)
            return shown

    def capture_observations(self, snapshot, sources) -> ReadObservations:
        """Prepare exact conversation rows once; consumers retain their original source."""
        with self.snapshot() as connection:
            header = ReadHeaderRow.current(connection)
            messages = ReadMessageRow.selected(connection, snapshot, sources)
        return ReadDocument(messages, header.source, {}, header.notice).observations(snapshot)

    def membership(self, viewer, snapshot, source: ReadSource):
        participant = snapshot.participant(viewer)
        with self.reading(OneViewer(participant.incarnation)) as document:
            return document.observations(snapshot).for_source(source).get(participant.incarnation.name, ReadMask())

    def displayed_recipient(self, message, recipient, snapshot, *, readers):
        name = snapshot.canonical_name(recipient.canonical_thread)
        thread = snapshot.threads.get(name)
        if thread is None or not self.human(thread.role):
            return ()
        return self._displayed_recipient(message, recipient, snapshot, readers)

    def historical_displayed_recipient(self, message, recipient, snapshot, *, readers):
        return self._displayed_recipient(message, recipient, snapshot, readers)

    @staticmethod
    def _displayed_recipient(message, recipient, snapshot, readers: Mapping[str, ReadMask]):
        from .bus_publication import stable_thread_lookup
        name = snapshot.canonical_name(recipient.canonical_thread)
        thread = snapshot.threads.get(name)
        if (thread is not None and stable_thread_lookup(thread.created_at) == recipient.recipient_lookup
                and readers.get(name, ReadMask()).contains(message)):
            return (thread.incarnation,)
        return ()

    def remove_threads(self, threads: Sequence[Thread]):
        if not threads:
            return
        births = tuple(thread.created_at for thread in threads)
        names = tuple(thread.name for thread in threads)
        sources = tuple(str(Path(thread.session_file).resolve()) for thread in threads if thread.session_file)
        with self.writing() as connection:
            ReadHeaderRow.current(connection)
            ReadMessageRow.delete(connection, viewer_birth=In(births))
            ReadTranscriptRow.delete(connection, viewer_name=In(names))
            ReadTranscriptRow.delete(connection, source_name=In(sources))

    def transcript_seen(self, viewer, source, inode):
        key = TranscriptKey.capture(viewer, source, inode)
        with self.snapshot() as connection:
            ReadHeaderRow.current(connection)
            row = ReadTranscriptRow.one(connection, key=key)
            return row.through if row is not None else 0

    def mark_transcript(self, viewer, source, inode, through, size):
        key = TranscriptKey.capture(viewer, source, inode)
        with self.writing() as connection:
            ReadHeaderRow.current(connection)
            previous = ReadTranscriptRow.one(connection, key=key)
            if previous is not None and previous.through <= size and previous.through >= through:
                return
            ReadTranscriptRow(key, through).upsert(connection)
