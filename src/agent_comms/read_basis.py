"""A9: immutable evidence for exactly the messages a client displays.

This is a value contract, not an assertion that fetching a page paints it. A
client acknowledges it only after paint. DM participant identity deliberately
uses ThreadIncarnation; process and turn generations do not alter provenance.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING, TextIO, cast

from .channel_targets import CommandTarget
from .coordination_errors import DMPaintedBoundChanged, DMScopeChanged, HistoricalSourceChanged
from .declared_family import DeclaredFamily
from .errors import UnregisteredThreadError
from .field_codec import FieldCodec
from .private_path import FileRevision
from .store_files import SharedLock, _store_lock
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation


class DisplayViewer(ABC):
    """The reader owns read evidence, aliases and cursor selection."""

    @abstractmethod
    def resolve(self, snapshot: RegistryProvenance) -> DisplayViewer: ...

    @abstractmethod
    def display_evidence(self, page, presentation, historical, source, index, document, snapshot) -> HistoricalDisplay | None: ...

    @abstractmethod
    def cursor(self, reads, snapshot, document, source) -> ReadCursor: ...

    @abstractmethod
    def names(self, snapshot) -> frozenset[str]: ...

    @abstractmethod
    def require_name(self, snapshot) -> str: ...

    @abstractmethod
    def read_page(self, scope, bounds, basis, source) -> MessagePage: ...


@dataclass(frozen=True, slots=True)
class AnonymousViewer(DisplayViewer):
    def resolve(self, snapshot: RegistryProvenance) -> AnonymousViewer:
        return self

    def display_evidence(self, page, presentation, historical, source, index, document, snapshot) -> None:
        return None

    def cursor(self, reads, snapshot, document, source) -> ReadCursor:
        return ReadCursor()

    def names(self, snapshot) -> frozenset[str]:
        return frozenset()

    def require_name(self, snapshot) -> str:
        raise ValueError("Viewer metrics require a named viewer")

    def read_page(self, scope, bounds, basis, source) -> MessagePage:
        return replace(bounds.conversation(scope).read_opened(source), display_scope=scope)


@dataclass(frozen=True, slots=True)
class NamedViewer(DisplayViewer):
    name: str

    def resolve(self, snapshot: RegistryProvenance) -> NamedViewer:
        return type(self)(snapshot.canonical_name(self.name))

    def display_evidence(self, page, presentation, historical, source, index, document, snapshot) -> HistoricalDisplay:
        historical.validate()
        return HistoricalDisplay(historical, presentation.bus.reads.capture_indexed(
            self.resolve(snapshot).name, page.messages, snapshot, source, index,
            conversation_snapshot=historical.provenance, document=document))

    def cursor(self, reads, snapshot, document, source) -> ReadCursor:
        return document.cursor(self.name, snapshot, source)

    def names(self, snapshot) -> frozenset[str]:
        return snapshot.names_for(self.name)

    def require_name(self, snapshot) -> str:
        return snapshot.canonical_name(self.name)

    def read_page(self, scope, bounds, basis, source) -> MessagePage:
        result = bounds.conversation(scope).displayed(
            basis.reads, basis.document, self.name, basis.registry, basis.registry, source,
        )
        return replace(result.page, display_scope=replace(scope, displayed=result.displayed))


class DisplaySelection(ABC):
    """The conversation selected by an original page or store observation."""

    __slots__ = ()

    @property
    @abstractmethod
    def has_evidence(self) -> bool: ...

    def select(self, page, sequences):
        selected = frozenset(sequences)
        return self.select_evidence(replace(page,
            messages=tuple(message for message in page.messages if message.seq in selected),
            source_pages=tuple(source.select(selected) for source in page.source_pages)), selected)

    @abstractmethod
    def select_evidence(self, page, selected): ...


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectedDisplay(DisplaySelection):
    viewer: ThreadIncarnation
    history_revision: FileRevision | None

    def page_selection(self) -> SelectedDisplay:
        return (HistoricalSelection(viewer=self.viewer, history_revision=self.history_revision)
                if self.history_revision is not None else self)

    @property
    def has_evidence(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class NoDisplaySelection(DisplaySelection):
    def select_evidence(self, page, selected):
        return page

    @property
    def has_evidence(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ChannelSelection(SelectedDisplay):
    bus_identity: tuple[int, int] | None
    scope: ChannelDisplayScope

    def select_evidence(self, page, selected):
        return replace(page, display_scope=replace(self.scope,
            displayed=self.scope.require_displayed().select(selected)))


@dataclass(frozen=True, slots=True)
class DMSelection(SelectedDisplay):
    root_identity: tuple[int, int]
    bus_identity: tuple[int, int] | None
    worktree: str
    requested_peer: str
    peer: ThreadIncarnation
    scope: DMDisplayScope

    def select_evidence(self, page, selected):
        basis = page.display_basis
        return replace(page, display_basis=replace(basis, displayed=basis.displayed.select(selected)))


@dataclass(frozen=True, slots=True)
class HistoricalSelection(SelectedDisplay):
    """The original viewer and attached-source revision select an archive page."""

    def select_evidence(self, page, selected):
        return replace(page, historical_display=page.historical_display.select(selected))


@dataclass(frozen=True, slots=True)
class HistoricalDisplay:
    source: HistorySource
    displayed: DisplayBasis

    def selection(self, history_revision: FileRevision | None) -> HistoricalSelection:
        return HistoricalSelection(
            viewer=self.displayed.viewer_identity, history_revision=history_revision,
        )

    def select(self, sequences) -> HistoricalDisplay:
        return HistoricalDisplay(self.source, self.displayed.select(sequences))

    def acknowledge(self, archive: HistoryArchive, registry: Registration) -> None:
        """Acknowledge exactly painted historical membership in its own ledger."""
        viewer = registry.require(self.displayed.viewer)
        viewer.role.require_user(DMScopeChanged("Historical viewer changed; refresh history"))
        if viewer.incarnation != self.displayed.viewer_identity:
            raise DMScopeChanged("Historical viewer changed; refresh history")
        if self.source not in archive.sources():
            raise HistoricalSourceChanged("Historical source detached; refresh history")
        self.source.acknowledge(self.displayed)


class LineOutput:
    """Flush once per event-loop pass."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.pending: asyncio.Handle | None = None

    def write(self, line: str) -> None:
        self.stream.write(line + '\n')
        if self.pending is None:
            self.pending = asyncio.get_running_loop().call_soon(self.flush)

    def flush(self) -> None:
        self.pending = None
        self.stream.flush()


class WatchAcknowledgment(DeclaredFamily, affix="WatchAcknowledgment"):
    """Who confirms that a streamed message reached its receiver."""

    @classmethod
    def line_output(cls, output: TextIO) -> Callable[[str], None]:
        return partial(print, file=output, flush=True)  # Visibility precedes implicit acknowledgment.

    @classmethod
    @abstractmethod
    def taken(cls, reads, displayed: DisplayBasis) -> None: ...


class OnReadWatchAcknowledgment(WatchAcknowledgment):
    @classmethod
    def taken(cls, reads, displayed: DisplayBasis) -> None:
        reads.mark_displayed(displayed.viewer, displayed)


class ExplicitWatchAcknowledgment(WatchAcknowledgment):
    @classmethod
    def line_output(cls, output: TextIO) -> Callable[[str], None]:
        return LineOutput(output).write

    @classmethod
    def taken(cls, reads, displayed: DisplayBasis) -> None:
        """The receiver confirms exact references through the existing read owner."""


if TYPE_CHECKING:
    from .bus_page_index import BusPageIndex, BusPageRow
    from .channels import Channel
    from .historical_views import HistoryArchive, HistorySource
    from .message_bus import MessageBus
    from .message_page import MessagePage, PageBounds
    from .messages import Message
    from .read_ledger import ReadLedger
    from .registration import Registration
    from .registry_document import RegistrySnapshot
    from .registry_provenance import RegistryProvenance
    from .wire_log import OpenedWireSource


@dataclass(frozen=True, slots=True)
class ConversationCursor:
    """One conversation's proved prefix and ordered out-of-order reads."""

    high_water: int = 0
    exceptions: tuple[int, ...] = ()

    @classmethod
    def union(cls, cursors: Iterable[ConversationCursor]) -> ConversationCursor:
        cursors = tuple(cursors)
        through = max((cursor.high_water for cursor in cursors), default=0)
        return cls(through, tuple(sorted({sequence for cursor in cursors
                                          for sequence in cursor.exceptions if sequence > through})))

    def add(self, sequences: tuple[int, ...]) -> ConversationCursor:
        return type(self)(self.high_water, tuple(sorted(
            n for n in set(self.exceptions).union(sequences) if n > self.high_water
        )))

    def through(self, sequence: int) -> ConversationCursor:
        sequence = max(sequence, self.high_water)
        return type(self)(sequence, self.exceptions[bisect_right(self.exceptions, sequence):])

    def __contains__(self, sequence: int) -> bool:
        index = bisect_left(self.exceptions, sequence)
        return 0 < sequence <= self.high_water or (index < len(self.exceptions) and self.exceptions[index] == sequence)


@dataclass(frozen=True, slots=True)
class ReadCursor:
    """Read evidence stays keyed by conversation even when viewer aliases join."""

    conversations: dict[Conversation, ConversationCursor] = field(default_factory=dict)

    def lower_bound(self, scope: ChannelDisplayScope, snapshot: RegistryProvenance) -> int:
        return scope.lower_bound(self, snapshot)

    @staticmethod
    def sql(parameter: str) -> str:
        return f"""read_cursors AS MATERIALIZED (
        SELECT json_extract(value,'$[0]') AS actor,
               json_extract(value,'$[1]') AS target,
               json_extract(value,'$[2]') AS sender,
               json_extract(value,'$[3]') AS high_water,
               json_extract(value,'$[4]') AS exceptions
        FROM json_each({parameter}))"""

    unread_sql = "seq > high_water AND seq NOT IN (SELECT value FROM json_each(exceptions))"

    joined_sql = """cursors AS MATERIALIZED (
        SELECT row.*,coalesce(read.high_water,0) AS high_water,
            coalesce(read.exceptions,'[]') AS exceptions FROM requests AS row
        LEFT JOIN read_cursors AS read ON read.actor=row.actor AND read.target=row.target
            AND (read.sender='' OR read.sender=row.sender)
    )"""

    def counted(self, scope: MessageDisplayScope, snapshot: RegistryProvenance
                ) -> Iterable[tuple[Conversation, ConversationCursor]]:
        return scope.counted(self, snapshot)

    @staticmethod
    def sql_bindings(snapshot: RegistryProvenance,
                     cursors: Mapping[str, Iterable[tuple[Conversation, ConversationCursor]]]
                     ) -> dict[str, str]:
        return {"read_cursors": json.dumps([
            (actor, target, sender, cursor.high_water, cursor.exceptions)
            for actor, selected in cursors.items() for conversation, cursor in selected
            for target, sender in conversation.sql_keys(snapshot)
        ])}

    @classmethod
    def union(cls, cursors: Iterable[ReadCursor]) -> ReadCursor:
        grouped: dict[Conversation, list[ConversationCursor]] = {}
        for cursor in cursors:
            for conversation, value in cursor.conversations.items():
                grouped.setdefault(conversation, []).append(value)
        return cls({key: ConversationCursor.union(values) for key, values in grouped.items()})

    def for_conversation(self, conversation: Conversation) -> ConversationCursor:
        return self.conversations.get(conversation, ConversationCursor())

    def add(self, displayed: Iterable[DisplayedConversation]) -> ReadCursor:
        conversations = dict(self.conversations)
        for item in displayed:
            previous = conversations.get(item.conversation, ConversationCursor())
            conversations[item.conversation] = previous.through(item.prefix_through).add(item.sequences)
        return type(self)(conversations)

    def current(self, snapshot: RegistryProvenance) -> ReadCursor:
        return type(self).union(type(self)({key.resolved(snapshot): value})
            for key, value in self.conversations.items() if key.current(snapshot))


class Conversation(DeclaredFamily, ABC, affix="Conversation"):
    family_discriminator = "kind"
    participants: tuple[ThreadIncarnation, ...] = ()

    def current(self, snapshot: RegistryProvenance) -> bool:
        return all(participant.current(snapshot) for participant in self.participants)

    @abstractmethod
    def resolved(self, snapshot: RegistryProvenance) -> Conversation: ...

    @abstractmethod
    def scope(self, snapshot: RegistryProvenance) -> ChannelDisplayScope | DMDisplayScope: ...

    @abstractmethod
    def sql_keys(self, snapshot: RegistryProvenance) -> Iterable[tuple[str, str]]: ...

    @abstractmethod
    def recorded_keys(self, snapshot: RegistryProvenance) -> Iterable[Conversation]: ...

    @abstractmethod
    def in_targets(self, targets: Collection[str], snapshot: RegistryProvenance) -> bool: ...


@dataclass(frozen=True, slots=True)
class ChannelConversation(Conversation):
    target: str

    def resolved(self, snapshot: RegistryProvenance) -> ChannelConversation:
        return self

    def scope(self, snapshot: RegistryProvenance) -> ChannelDisplayScope:
        return ChannelDisplayScope(self.target, frozenset((self.target,)))

    def sql_keys(self, snapshot: RegistryProvenance) -> Iterable[tuple[str, str]]:
        return ((self.target, ""),)

    def recorded_keys(self, snapshot: RegistryProvenance) -> Iterable[Conversation]:
        return (self,)

    def in_targets(self, targets: Collection[str], snapshot: RegistryProvenance) -> bool:
        return self.target in targets


@dataclass(frozen=True, slots=True)
class DMConversation(Conversation):
    participants: tuple[ThreadIncarnation, ...] = ()

    def resolved(self, snapshot: RegistryProvenance) -> DMConversation:
        return type(self)(tuple(sorted(
            (participant.resolved(snapshot) for participant in self.participants),
            key=lambda participant: participant.name,
        )))

    def scope(self, snapshot: RegistryProvenance) -> DMDisplayScope:
        first, second = self.participants
        return DMDisplayScope.capture(first.name, second.name, snapshot)

    def recorded_keys(self, snapshot: RegistryProvenance) -> Iterable[Conversation]:
        return (type(self)(tuple(sorted(pair, key=lambda p: p.name)))
                for pair in product(*(p.recorded_names(snapshot) for p in self.participants)))

    def in_targets(self, targets: Collection[str], snapshot: RegistryProvenance) -> bool:
        return any(snapshot.names_for(p.name).intersection(targets) for p in self.participants)

    def sql_keys(self, snapshot: RegistryProvenance) -> Iterable[tuple[str, str]]:
        first, second = self.participants
        a, b = snapshot.names_for(first.name), snapshot.names_for(second.name)
        return tuple((target, sender) for senders, targets in ((a, b), (b, a))
                     for sender in senders for target in targets)


@dataclass(frozen=True, slots=True)
class DisplayedConversation:
    conversation: Conversation
    sequences: tuple[int, ...]
    prefix_through: int = 0

    def select(self, selected: Collection[int]) -> DisplayedConversation:
        omitted = min((n for n in self.sequences if n not in selected), default=self.prefix_through + 1)
        return type(self)(self.conversation, tuple(n for n in self.sequences if n in selected),
                          min(self.prefix_through, omitted - 1))

    def through(self, sequence: int) -> DisplayedConversation:
        return type(self)(self.conversation, tuple(n for n in self.sequences if n <= sequence),
                          min(self.prefix_through, sequence))


@dataclass(frozen=True, slots=True)
class DisplayBasis:
    viewer: str
    viewer_created_at: float
    conversations: tuple[DisplayedConversation, ...]
    bus_identity: tuple[int, int] | None

    @property
    def viewer_identity(self) -> RecordedThreadIncarnation:
        return RecordedThreadIncarnation(self.viewer, self.viewer_created_at)

    @property
    def message_count(self) -> int:
        return sum(len(item.sequences) for item in self.conversations)

    def select(self, sequences: Collection[int]) -> DisplayBasis:
        """Retain painted members of this proof; never introduce new sequences."""
        selected = frozenset(sequences)
        return DisplayBasis(
            self.viewer,
            self.viewer_created_at,
            tuple(
                item.select(selected)
                for item in self.conversations
            ),
            self.bus_identity,
        )

    def through(self, sequence: int) -> DisplayBasis:
        if sequence < 0:
            raise ValueError("Display sequence cannot be negative.")
        return DisplayBasis(
            self.viewer,
            self.viewer_created_at,
            tuple(
                item.through(sequence)
                for item in self.conversations
            ),
            self.bus_identity,
        )

    def validate(
        self, viewer: str, snapshot: RegistrySnapshot, bus_identity: tuple[int, int] | None
    ) -> None:
        thread = snapshot.threads.get(viewer)
        if (
            viewer != self.viewer
            or thread is None
            or thread.incarnation != self.viewer_identity
            or bus_identity != self.bus_identity
            or any(not item.conversation.current(snapshot) for item in self.conversations)
        ):
            raise DMScopeChanged("Display incarnation changed; refresh the displayed page.")


@dataclass(frozen=True, slots=True)
class DMDisplayBasis(DeclaredFamily, affix="DMDisplayBasis"):
    """Immutable identity/scope witness for one fetched human DM page.

    A page fetch is not proof that a UI painted it. The client must supply
    separate contiguous painted-tail evidence before requesting a read mark.
    """

    root: str
    root_identity: tuple[int, int]
    worktree: str
    requested_peer: str
    viewer_identity: ThreadIncarnation
    viewer_names: frozenset[str]
    peer_identity: ThreadIncarnation
    peer_names: frozenset[str]
    bus_identity: tuple[int, int] | None
    displayed: DisplayBasis
    fetched_through: int

    @classmethod
    def fetch_page(
        cls,
        bus: MessageBus,
        registry: Registration,
        root: Path,
        viewer: str,
        peer: str,
        *,
        worktree: str,
        bounds: PageBounds,
    ) -> MessagePage:
        """Fetch a bounded human DM page and its immutable identity/read basis.

        Fetching is not paint proof. A UI may use the basis only after proving
        that the corresponding inbound tail was contiguous and visibly painted.
        """
        peer = CommandTarget.named(peer).require_dm_peer()
        with _store_lock(root / "wire", mode=SharedLock()), registry.store.reading() as registry_document:
            snapshot = registry_document.snapshot()
            viewer_name = snapshot.canonical_name(viewer)
            peer_name = snapshot.canonical_name(peer)
            scope = DMDisplayScope.capture_human(viewer_name, peer_name, snapshot)
            viewer_thread = snapshot.threads[viewer_name]
            peer_thread = snapshot.participant(peer_name)
            viewer_names, peer_names = scope.first_names, scope.second_names
            revision = registry.store.revision_unlocked()
            document = bus.reads.read_conversation(viewer_name, scope.read_key(viewer_name, peer_name, snapshot), snapshot)
        from .wire_log import CurrentSnapshotOpening
        with bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as source:
            result = bounds.conversation(scope).displayed(
                bus.reads, document, viewer_name, snapshot, snapshot, source,
            )
        page, displayed = result.page, result.displayed
        with _store_lock(root / "wire", mode=SharedLock()):
            kind = CoveredDMDisplayBasis if displayed.message_count else EmptyDMDisplayBasis
            root_info = root.stat()
            basis = kind(
                root=str(root.resolve()),
                root_identity=(root_info.st_dev, root_info.st_ino),
                worktree=str(Path(worktree).resolve()),
                requested_peer=peer,
                viewer_identity=viewer_thread.incarnation,
                viewer_names=viewer_names,
                peer_identity=peer_thread.incarnation,
                peer_names=peer_names,
                bus_identity=displayed.bus_identity,
                displayed=displayed,
                fetched_through=page.newest_seq or 0,
            )
            with registry.store.locked(mode=SharedLock()):
                if registry.store.revision_unlocked() != revision:
                    raise HistoricalSourceChanged("Registry changed; refresh the DM page")
                basis.validate_identity(snapshot, bus.reads.bus_identity(bus.log.path))
            return replace(page, display_basis=basis)

    def selection(self, history_revision: FileRevision | None) -> SelectedDisplay:
        return DMSelection(
            root_identity=self.root_identity, bus_identity=self.bus_identity,
            worktree=self.worktree, requested_peer=self.requested_peer,
            viewer=self.viewer_identity, peer=self.peer_identity,
            scope=DMDisplayScope(self.viewer_names, self.peer_names),
            history_revision=history_revision,
        ).page_selection()

    @property
    def viewer(self) -> str:
        return self.viewer_identity.name

    @property
    def viewer_created_at(self) -> float:
        return self.viewer_identity.created_at

    @property
    def peer(self) -> str:
        return self.peer_identity.name

    @property
    def peer_created_at(self) -> float:
        return self.peer_identity.created_at

    def acknowledge(
        self,
        bus: MessageBus,
        registry: Registration,
        root: Path,
        peer: str,
        *,
        worktree: str,
        through: int,
    ) -> None:
        """CAS one painted human DM tail, never a global/executor ACK.

        The page must have no omitted unread inbound messages. The caller must
        additionally prove its through-bound was actually and contiguously
        painted; a fetched page alone cannot establish visibility.
        """
        if type(through) is not int:
            raise ValueError("Painted DM read requires a typed page basis and integer bound.")
        with _store_lock(root / "wire", mode=SharedLock()), _store_lock(registry.store.path):
            snapshot = registry.store._read_unlocked().snapshot()
            self.validate_for(
                root,
                worktree,
                peer,
                through,
                snapshot,
                bus.reads.bus_identity(bus.log.path),
            )
            self.mark(bus.reads, through)

    @abstractmethod
    def mark(self, reads: ReadLedger, through: int) -> None: ...

    @abstractmethod
    def validate_tail(self, through: int) -> None: ...

    def validate_for(
        self,
        root: Path,
        worktree: str,
        peer: str,
        through: int,
        snapshot: RegistrySnapshot,
        bus_identity: tuple[int, int] | None,
    ) -> None:
        if (self.root, self.worktree, self.requested_peer) != (
            str(root.resolve()),
            str(Path(worktree).resolve()),
            peer,
        ):
            raise DMScopeChanged("Painted DM read belongs to a different root, worktree or peer.")
        self.validate_tail(through)
        info = root.stat()
        if (info.st_dev, info.st_ino) != self.root_identity:
            raise DMScopeChanged("DM root was replaced; refresh the page.")
        self.validate_identity(snapshot, bus_identity)

    def validate_identity(
        self, snapshot: RegistrySnapshot, bus_identity: tuple[int, int] | None
    ) -> None:
        """Fetch and painted acknowledgement share actual identity/scope validation.

        Heartbeat, turn state and another view's read progress are not identity.
        """
        try:
            selected = next(
                human for thread in snapshot.threads.values() for human in thread.role.human(thread)
            )
            viewer = snapshot.threads[self.viewer]
            target = snapshot.participant(self.requested_peer)
            scope = DMDisplayScope.capture_human(self.viewer, self.requested_peer, snapshot)
        except (KeyError, StopIteration, UnregisteredThreadError) as error:
            raise DMScopeChanged("DM viewer/peer incarnation changed; refresh the page.") from error
        if (
            selected.incarnation != self.viewer_identity
            or (viewer.incarnation, target.incarnation)
            != (self.viewer_identity, self.peer_identity)
            or scope != DMDisplayScope(self.viewer_names, self.peer_names)
        ):
            raise DMScopeChanged("DM viewer/peer incarnation changed; refresh the page.")
        if bus_identity != self.bus_identity:
            raise DMScopeChanged("DM bus was replaced; refresh the page.")
        self.displayed.validate(self.viewer, snapshot, bus_identity)


class CoveredDMDisplayBasis(DMDisplayBasis):
    """Only fetched references become read; omitted older messages stay unread."""

    def validate_tail(self, through: int) -> None:
        if through > self.fetched_through:
            raise DMPaintedBoundChanged()

    def mark(self, reads: ReadLedger, through: int) -> None:
        reads.mark_displayed(self.viewer, self.displayed.through(through))


class EmptyDMDisplayBasis(DMDisplayBasis):
    """An empty range validates its identity but acknowledges nothing."""

    def validate_tail(self, through: int) -> None:
        pass

    def mark(self, reads: ReadLedger, through: int) -> None:
        pass


class MessageDisplayScope(ABC):
    """Captured inclusion and its sound candidate reduction for the page index."""

    def read_key(self, sender: str, target: str, snapshot: RegistryProvenance) -> Conversation:
        return CommandTarget.named(target).conversation_key(
            sender, snapshot, ChannelConversation, DMConversation
        )

    def has_read(self, seen: ReadCursor, message: Message | BusPageRow,
                 snapshot: RegistryProvenance) -> bool:
        conversation = self.read_key(message.sender, message.target, snapshot)
        return message.seq in seen.for_conversation(conversation)

    def offsets(self, index, *, lower, upper, descending, before_offset):
        return index.offsets(lower=lower, upper=upper, descending=descending,
                             predicate=self.index_predicate, before_offset=before_offset)

    @property
    @abstractmethod
    def index_predicate(self) -> tuple[str, tuple]:
        """Declare the candidates covered by this scope's index query."""

    @property
    @abstractmethod
    def index_targets(self) -> frozenset[str]:
        """The explicitly addressed targets included by this scope."""

    def counted(self, cursor: ReadCursor, snapshot: RegistryProvenance
                ) -> Iterable[tuple[Conversation, ConversationCursor]]:
        targets = self.index_targets
        return ((key, value) for key, value in cursor.conversations.items()
                if key.in_targets(targets, snapshot))

    @abstractmethod
    def includes(self, message: Message) -> bool:
        """Apply the captured predicate to a validated authoritative message."""


@dataclass(frozen=True, slots=True)
class DMDisplayScope(MessageDisplayScope):
    first_names: frozenset[str]
    second_names: frozenset[str]

    @classmethod
    def capture(cls, a: str, b: str, snapshot: RegistryProvenance) -> DMDisplayScope:
        return cls(snapshot.names_for(a), snapshot.names_for(b))

    @classmethod
    def capture_human(cls, viewer: str, peer: str, snapshot: RegistrySnapshot) -> DMDisplayScope:
        try:
            first = snapshot.threads[snapshot.canonical_name(viewer)]
            second = snapshot.participant(peer)
        except (KeyError, UnregisteredThreadError) as error:
            raise DMScopeChanged("DM display identity changed; refresh the page.") from error
        first.role.require_user(DMScopeChanged("DM display identity changed; refresh the page."))
        if first.incarnation == second.incarnation:
            raise DMScopeChanged("DM display identity changed; refresh the page.")
        return cls.capture(first.name, second.name, snapshot)

    @property
    def index_predicate(self) -> tuple[str, tuple]:
        first, second = tuple(self.first_names), tuple(self.second_names)
        a, b = ",".join("?" for _ in first), ",".join("?" for _ in second)
        return (f"((sender IN ({a}) AND target IN ({b})) OR "
                f"(sender IN ({b}) AND target IN ({a})))", (*first, *second, *second, *first))

    @property
    def index_targets(self) -> frozenset[str]:
        return self.first_names | self.second_names

    def includes(self, message: Message) -> bool:
        return (message.sender in self.first_names and message.target in self.second_names) or (
            message.sender in self.second_names and message.target in self.first_names
        )


@dataclass(frozen=True, slots=True)
class ListedDeliveries(MessageDisplayScope):
    """The messages of another scope whose sequences the route index listed."""

    scope: MessageDisplayScope
    sequences: frozenset[int]
    targets: frozenset[str]

    @property
    def index_targets(self) -> frozenset[str]:
        return self.targets

    @property
    def index_predicate(self) -> tuple[str, tuple]:
        return "seq IN (" + ",".join("?" for _ in self.sequences) + ")", tuple(self.sequences)

    def includes(self, message: Message) -> bool:
        return message.seq in self.sequences and self.scope.includes(message)


@dataclass(frozen=True, slots=True)
class ChannelDisplayScope(MessageDisplayScope):
    """One captured local display predicate; it never changes delivery or history ownership."""

    channel: str
    targets: frozenset[str]
    displayed: DisplayBasis | None = field(default=None, compare=False)

    @classmethod
    def capture(
        cls, channel: Channel, snapshot: RegistryProvenance
    ) -> ChannelDisplayScope:
        if channel.any_mode:
            return MemberActivityDisplayScope.capture(channel, snapshot)
        return cls.history(channel)

    @classmethod
    def history(cls, channel: Channel) -> ChannelDisplayScope:
        if channel.aggregate:
            return AllChannelDisplayScope(channel.name)
        # Nonaggregate channel declarations always supply their finite targets.
        return cls(channel.name, cast(frozenset[str], channel.history_targets))

    @property
    def index_targets(self) -> frozenset[str]:
        return self.targets

    def lower_bound(self, cursor: ReadCursor, snapshot: RegistryProvenance) -> int:
        return min((cursor.for_conversation(ChannelConversation(target)).high_water
                    for target in self.index_targets), default=0)

    @property
    def index_predicate(self) -> tuple[str, tuple]:
        targets = tuple(self.index_targets)
        return "target IN (" + ",".join("?" for _ in targets) + ")", targets

    def capture_all(self, reads: ReadLedger, viewer: str, snapshot: RegistrySnapshot,
                    source: OpenedWireSource, index: BusPageIndex, *, document) -> DisplayBasis:
        return DisplayBasis(viewer, snapshot.require(viewer).created_at,
            tuple(DisplayedConversation(ChannelConversation(target), (), row.seq)
                  for target in self.targets
                  if (row := index.latest_target(target, source.boundary)) is not None),
            source.bus_identity)

    def selection(self, history_revision: FileRevision | None) -> DisplaySelection:
        if self.displayed is None:
            return NoDisplaySelection()
        return ChannelSelection(
            viewer=self.displayed.viewer_identity, bus_identity=self.displayed.bus_identity,
            scope=self, history_revision=history_revision,
        ).page_selection()

    def require_displayed(self) -> DisplayBasis:
        if self.displayed is None:
            raise DMPaintedBoundChanged()
        return self.displayed

    def includes(self, message: Message) -> bool:
        return message.target in self.targets


@dataclass(frozen=True, slots=True)
class AllChannelDisplayScope(ChannelDisplayScope):
    """A complete wire view cannot reduce cursors to explicit target names."""

    targets: frozenset[str] = frozenset()

    def counted(self, cursor: ReadCursor, snapshot: RegistryProvenance
                ) -> Iterable[tuple[Conversation, ConversationCursor]]:
        return cursor.conversations.items()

    def lower_bound(self, cursor: ReadCursor, snapshot: RegistryProvenance) -> int:
        return 0

    @property
    def index_predicate(self) -> tuple[str, tuple]:
        return "1", ()

    def includes(self, message: Message) -> bool:
        return True

    def capture_all(self, reads: ReadLedger, viewer: str, snapshot: RegistrySnapshot,
                    source: OpenedWireSource, index: BusPageIndex, *, document) -> DisplayBasis:
        return reads.capture_indexed(viewer, index.scope_rows(self, source.boundary),
                                     snapshot, source, index, document=document)


@dataclass(frozen=True, slots=True)
class MemberActivityDisplayScope(AllChannelDisplayScope):
    """Member activity may match a sender, DM peer or mention on any target."""

    participant_names: frozenset[str] = frozenset()

    @classmethod
    def capture(cls, channel: Channel, snapshot: RegistryProvenance) -> MemberActivityDisplayScope:
        members = frozenset(name for name, thread in snapshot.threads.items()
                            if channel.tags <= thread.tags)
        names = members | frozenset(alias for alias, owner in snapshot.aliases.items()
                                    if owner in members)
        return cls(channel.name, cast(frozenset[str], channel.history_targets), participant_names=names)

    @property
    def index_predicate(self) -> tuple[str, tuple]:
        targets, names = tuple(self.targets), tuple(self.participant_names)
        target_marks, name_marks = ",".join("?" for _ in targets), ",".join("?" for _ in names)
        return (f"target IN ({target_marks}) OR sender IN ({name_marks}) OR "
                f"(target NOT LIKE '#%' AND target IN ({name_marks})) OR "
                f"EXISTS (SELECT 1 FROM json_each(mentions) WHERE value IN ({name_marks}))",
                (*targets, *names, *names, *names))

    def includes(self, message: Message) -> bool:
        return (message.target in self.targets or message.sender in self.participant_names
                or any(name in self.participant_names
                       for name in CommandTarget.named(message.target).direct_names)
                or any(mention.thread in self.participant_names for mention in message.mentions))
