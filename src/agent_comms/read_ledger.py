"""Human read authority: typed conversation identities and compact exact progress."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .typed_table import Column, Index, TypedStore, TypedTable
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

    def memberships(self, snapshot: RegistryProvenance, source: FileIdentity) -> dict[str, ReadMask]:
        if self.bus_identity != (source.device, source.inode):
            return {}
        viewers = {}
        for key, state in self.messages.items():
            if not key.viewer.current(snapshot) or not key.conversation.current(snapshot):
                continue
            key = key.resolved(snapshot)
            conversations = viewers.setdefault(key.viewer.name, {})
            conversations[key.conversation] = conversations.get(key.conversation, ReadProgress()).merge(state)
        incarnations = {
            identity.name: participant.incarnation
            for participant in snapshot.threads.values()
            for identity in participant.incarnation.recorded_names(snapshot)
        }
        return {viewer: ReadMask(conversations, incarnations)
                for viewer, conversations in viewers.items()}


class ReadSource(DeclaredFamily, affix='ReadSource'):
    @classmethod
    def from_revision(cls, revision):
        return AbsentReadSource() if revision is None else PresentReadSource(revision.identity)

    def memberships(self, document, snapshot):
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class PresentReadSource(ReadSource):
    identity: FileIdentity

    def memberships(self, document, snapshot):
        return document.memberships(snapshot, self.identity)


@dataclass(frozen=True, slots=True)
class AbsentReadSource(ReadSource):
    def memberships(self, document, snapshot):
        return {}


class ReadTable:
    schema: ClassVar[int] = 1


@dataclass(frozen=True)
class ReadHeaderRow(ReadTable, TypedTable):
    id: int = field(default=1, metadata={'sql': Column(primary_key=True)})
    bus_identity: tuple[int, int] | None = None
    notice: str | None = None

    @classmethod
    def current(cls, connection):
        rows = cls.select(connection)
        if not rows:
            return cls()
        current, = rows
        return current


@dataclass(frozen=True)
class ReadMessageRow(ReadTable, TypedTable):
    key: ConversationKey = field(metadata={'sql': Column(primary_key=True)})
    progress: ReadProgress
    viewer_birth: float = field(init=False, compare=False, repr=False,
        metadata={'sql': Column(generated="json_extract(key,'$.viewer.created_at')")})
    target: str = field(init=False, compare=False, repr=False,
        metadata={'sql': Column(generated="json_extract(key,'$.conversation.target')")})
    indexes = (Index(('viewer_birth', 'target')),)
    without_rowid = True

    def remove(self, connection):
        key, = self._fields_named(('key',))
        connection.execute(f'DELETE FROM "{self.declared_name}" WHERE key=?',
                           (key.encode(self.key),))

    @classmethod
    def selected(cls, connection, snapshot, sources):
        """Decode progress only for the visible conversations and their viewers."""
        births, conversations = set(), set()
        for source in sources:
            message = source.message
            conversations.add(Conversation.from_message(message, snapshot).resolved(snapshot))
            names = {recipient.canonical_thread for recipient in source.audience.recipients}
            names.add(message.target)
            for name in names:
                participant = snapshot.threads.get(snapshot.canonical_name(name))
                if participant is not None:
                    births.add(participant.created_at)
        if not births or not conversations:
            return {}
        targets = {conversation.target for conversation in conversations}
        where = ('viewer_birth IN (' + ','.join('?' for _ in births) + ') AND target IN ('
                 + ','.join('?' for _ in targets) + ')')
        key_field, progress_field = cls._fields_named(('key', 'progress'))
        messages = {}
        for key_value, progress_value in connection.execute(
            f'SELECT key,progress FROM "{cls.declared_name}" WHERE {where}',
            (*births, *targets),
        ):
            key = key_field.decode(key_value)
            if key.conversation.resolved(snapshot) in conversations:
                messages[key] = progress_field.decode(progress_value)
        return messages


@dataclass(frozen=True)
class ReadTranscriptRow(ReadTable, TypedTable):
    key: TranscriptKey = field(metadata={'sql': Column(primary_key=True)})
    through: int
    viewer_name: str = field(init=False, compare=False, repr=False,
        metadata={'sql': Column(generated="json_extract(key,'$.viewer')", index=True)})
    without_rowid = True


class ReadViewers(DeclaredFamily, affix='Viewers'):
    def messages(self, connection):
        raise NotImplementedError

    def transcripts(self, connection):
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class AllViewers(ReadViewers):
    def messages(self, connection):
        return ReadMessageRow.select(connection)

    def transcripts(self, connection):
        return ReadTranscriptRow.select(connection)


@dataclass(frozen=True, slots=True)
class OneViewer(ReadViewers):
    incarnation: ThreadIncarnation

    def messages(self, connection):
        return ReadMessageRow.select(connection, viewer_birth=self.incarnation.created_at)

    def transcripts(self, connection):
        return ReadTranscriptRow.select(connection, viewer_name=self.incarnation.name)


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
            {row.key: row.progress for row in messages}, header.bus_identity,
            {row.key: row.through for row in transcripts}, header.notice,
        )

    @contextmanager
    def reading(self, viewers: ReadViewers):
        if not self.path.exists():
            yield ReadDocument()
            return
        with self.snapshot() as connection:
            yield self._document(connection, viewers)

    def read(self):
        with self.reading(AllViewers()) as document:
            return document

    @classmethod
    def _replace(cls, connection, document):
        for row in cls.rows:
            connection.execute(f'DELETE FROM "{row.declared_name}"')
        ReadHeaderRow(bus_identity=document.bus_identity, notice=document.notice).insert(connection)
        for key, progress in document.messages.items():
            ReadMessageRow(key, progress).insert(connection)
        for key, through in document.transcripts.items():
            ReadTranscriptRow(key, through).insert(connection)

    def replace(self, document):
        with self.writing() as connection:
            self._replace(connection, document)

    def _update(self, change):
        with self.writing() as connection:
            original = self._document(connection, AllViewers())
            changed = change(original)
            if changed is not original:
                self._replace(connection, changed)

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
                if header.bus_identity != displayed.bus_identity:
                    connection.execute(f'DELETE FROM "{ReadMessageRow.declared_name}"')
                    replace(header, bus_identity=displayed.bus_identity).upsert(connection)
                for item in displayed.conversations:
                    conversation = item.conversation.resolved(snapshot)
                    key = ConversationKey(displayed.viewer_identity.resolved(snapshot), conversation)
                    retained = tuple(row for row in ReadMessageRow.select(
                        connection, where='viewer_birth=? AND target=?',
                        parameters=(key.viewer.created_at, conversation.target),
                    ) if row.key.resolved(snapshot) == key)
                    state = ReadProgress()
                    for row in retained:
                        state = state.merge(row.progress)
                    marked = state.mark(item.sequences, lambda value: prefix(conversation, value))
                    if len(retained) == 1 and retained[0].key == key and marked == state:
                        continue
                    for row in retained:
                        row.remove(connection)
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

    def take_notice(self, shown):
        if shown is None:
            return None
        taken = None
        def take(document):
            nonlocal taken
            if shown is None or document.notice != shown:
                return document
            taken = shown
            return replace(document, notice=None)
        self._update(take)
        return taken

    def observations(self, snapshot, source: ReadSource, *, document=None):
        document = self.read() if document is None else document
        return source.memberships(document, snapshot)

    def observations_for(self, snapshot, sources, identity: ReadSource):
        """Read only visible conversation rows; never materialize the whole ledger."""
        if not sources or not self.path.exists():
            return {}
        with self.snapshot() as connection:
            header = ReadHeaderRow.current(connection)
            messages = ReadMessageRow.selected(connection, snapshot, sources)
        return identity.memberships(ReadDocument(messages, header.bus_identity), snapshot)

    def membership(self, viewer, snapshot, source: ReadSource, *, document=None):
        name = snapshot.canonical_name(viewer)
        participant = snapshot.threads.get(name)
        if participant is None:
            return ReadMask()
        if document is None:
            with self.reading(OneViewer(participant.incarnation)) as selected:
                return self.observations(snapshot, source, document=selected).get(name, ReadMask())
        return self.observations(snapshot, source, document=document).get(name, ReadMask())

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
        viewers = {thread.incarnation for thread in threads}
        names = {thread.name for thread in threads}
        sources = {str(Path(thread.session_file).resolve()) for thread in threads if thread.session_file}
        def remove(document):
            messages = {k: v for k, v in document.messages.items() if k.viewer not in viewers}
            transcripts = {k: v for k, v in document.transcripts.items() if k.source not in sources and k.viewer not in names}
            return (document if messages == document.messages and transcripts == document.transcripts
                    else replace(document, messages=messages, transcripts=transcripts))
        self._update(remove)

    def transcript_seen(self, viewer, source, inode, *, document=None):
        key = TranscriptKey.capture(viewer, source, inode)
        if document is not None:
            return document.transcripts.get(key, 0)
        if not self.path.exists():
            return 0
        with self.snapshot() as connection:
            row = ReadTranscriptRow.one(connection, key=key)
            return row.through if row is not None else 0

    def mark_transcript(self, viewer, source, inode, through, size):
        key = TranscriptKey.capture(viewer, source, inode)
        with self.writing() as connection:
            previous = ReadTranscriptRow.one(connection, key=key)
            if previous is not None and previous.through <= size and previous.through >= through:
                return
            ReadTranscriptRow(key, through).upsert(connection)
