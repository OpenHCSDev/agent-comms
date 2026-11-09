from __future__ import annotations

from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass, field
from sqlite3 import Connection
from typing import Annotated, ClassVar

from agent_comms.execution.native_record_file import RecordsCut
from agent_comms.execution.native_records import TranscriptRecord
from agent_comms.execution.turn_item import NativeItemId, NativeTurnId
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import PageRequestId, ThreadLookup
from agent_comms.storage.recorded_time import EventTime
from agent_comms.storage.typed_table import (
    Column,
    ForeignKey,
    Generated,
    Index,
    RealStorage,
    StoreRows,
    TypedRow,
    TypedTable,
)
from agent_comms.transcript.transcript_events import ToolStartTranscript, TranscriptEvent
from agent_comms.transport.native_tools import MutatingPathTool
from agent_comms.transport.tool_declaration import ToolDeclaration


class ReplyWindow(DeclaredFamily, affix="Replies"):
    """The turn owns its opening deliveries, before and after output starts."""

    def delivered(self, connection: Connection, turn: ExternalTurn,
                  references: tuple[MessageReference, ...]) -> None:
        """Context cannot reopen a retired turn's input window."""

    def answering(self) -> ReplyWindow:
        return self

    def references_in(self, connection: Connection, turn: ExternalTurn) -> tuple[MessageReference, ...]:
        return ()

    def answered(self, connection: Connection, turn: ExternalTurn) -> tuple[MessageReference, ...]:
        return ()

    def observed(self, events: tuple[TranscriptEvent, ...]) -> ReplyWindow:
        window = self
        for event in events:
            window = event.reply_window(window, window.answering)
        return window

    def finished(self) -> ReplyWindow:
        return self

    def cancelled(self) -> ReplyWindow:
        return self


class ActiveReplies(ReplyWindow, declared_name=None):
    """Only active windows receive context or transition on turn retirement."""

    def references_in(self, connection: Connection, turn: ExternalTurn) -> tuple[MessageReference, ...]:
        return turn.opening_references(connection)

    def finished(self) -> ReplyWindow:
        return FinishedReplies()

    def cancelled(self) -> ReplyWindow:
        return StoppedReplies()


@dataclass(frozen=True)
class OpeningReplies(ActiveReplies):
    def delivered(self, connection: Connection, turn: ExternalTurn,
                  references: tuple[MessageReference, ...]) -> None:
        for reference in references:
            ExternalTurnInput(turn.thread, turn.turn_id, reference).insert_if_absent(connection)

    def answering(self) -> ReplyWindow:
        return AnsweringReplies()


@dataclass(frozen=True)
class AnsweringReplies(ActiveReplies):
    """Later context cannot change which deliveries opened this answer."""


@dataclass(frozen=True)
class StoppedReplies(ReplyWindow):
    """A retired window cannot be reopened by input or output."""


class FinishedReplies(StoppedReplies):
    def answered(self, connection: Connection, turn: ExternalTurn) -> tuple[MessageReference, ...]:
        opening = turn.opening_references(connection)
        return opening if any(event.answers_turn
            for item in ExternalItemRow.select(connection, thread=turn.thread, turn_id=turn.turn_id,
                                                order_by=("seq",), descending=True, limit=1)
            for event in item.events_in(connection)) else ()


class ExternalPage(DeclaredFamily):
    """The continuation belonging to the oldest published item."""

    def has_older(self, offset: int, row) -> bool:
        return offset >= row.seq

    def request(self, transcript, row, offset: int) -> bool:
        return False

    def frames(self, row) -> tuple:
        return ()


@dataclass(frozen=True)
class NoEarlierPage(ExternalPage):
    pass


@dataclass(frozen=True)
class EarlierPage(ExternalPage):
    cursor: str

    def has_older(self, offset: int, row) -> bool:
        return True

    def request(self, transcript, row, offset: int) -> bool:
        if super().has_older(offset, row):
            return False  # Local records remain; the provider is not needed yet.
        return self.request_page(transcript, row)

    def request_page(self, transcript, row) -> bool:
        with transcript.writing() as connection:
            if transcript.oldest(connection) != row:
                return True  # The read retries against the newly published prefix.
            row.continue_with(connection, RequestedEarlierPage(self.cursor, PageRequestId.mint()))
        return True


@dataclass(frozen=True)
class RequestedEarlierPage(EarlierPage):
    """Demand is a transition of the stored continuation, not a second cursor store."""
    request_id: PageRequestId

    def request_page(self, transcript, row) -> bool:
        return True  # A deferred read keeps the request without another write.

    def frames(self, row) -> tuple:
        return (row,)


class NoItems:
    start = 0

    def has_records(self, through: int) -> bool:
        return False

    def valid_offset(self, offset: int, through: int) -> bool:
        return 0 <= offset <= through

    def has_older(self, offset: int) -> bool:
        return False

    def request_earlier(self, transcript, offset: int) -> bool:
        return False

    def frames(self) -> tuple:
        return ()

    def continue_with(self, connection, earlier: ExternalPage) -> None:
        pass

    def matches(self, item) -> bool:
        return False

    def pending(self, request) -> bool:
        return False


@dataclass(frozen=True)
class ExternalPageRow(TypedRow):
    thread: Annotated[ThreadLookup, Column(primary_key=True)]
    turn_id: Annotated[NativeTurnId, Column(primary_key=True)]
    item_id: Annotated[NativeItemId, Column(primary_key=True)]
    seq: int
    earlier: ExternalPage = field(default_factory=NoEarlierPage, kw_only=True)

    @property
    def start(self) -> int:
        return self.seq - 1

    def has_records(self, through: int) -> bool:
        return True  # Zero and negative positions name real prepended items.

    def valid_offset(self, offset: int, through: int) -> bool:
        return offset <= through

    def has_older(self, offset: int) -> bool:
        return self.earlier.has_older(offset, self)

    def request_earlier(self, transcript, offset: int) -> bool:
        return self.earlier.request(transcript, self, offset)

    def frames(self) -> tuple:
        return self.earlier.frames(self)

    def matches(self, item) -> bool:
        return (self.turn_id, self.item_id) == (item.turn_id, item.item_id)

    def pending(self, request) -> bool:
        return self.matches(request) and self.earlier == request.request

    def continue_with(self, connection, earlier: ExternalPage) -> None:
        ExternalItemRow.update(connection, where="thread=? AND turn_id=? AND item_id=?",
                               parameters=(self.thread, self.turn_id, self.item_id), earlier=earlier)


class ExternalTranscriptRows(StoreRows):
    """The tables of ExternalTranscript; each row declares itself one."""


@dataclass(frozen=True)
class ExternalTurn(ExternalTranscriptRows, TypedTable):
    """One external turn's opening deliveries, retained after its completion."""

    seq: Annotated[int, Column(primary_key=True, auto_increment=True)]
    thread: ThreadLookup
    turn_id: NativeTurnId
    replies: ReplyWindow
    unique = (("thread", "turn_id"),)
    indexes = (Index(("thread", "seq")), Index(("thread", "replies")))

    @classmethod
    def opened(cls, connection: Connection, thread: ThreadLookup, turn_id: NativeTurnId) -> ExternalTurn:
        for turn in cls.select(connection, thread=thread, turn_id=turn_id):
            return turn
        cls.insert_values(connection, thread=thread, turn_id=turn_id, replies=OpeningReplies())
        return cls.require(connection, thread=thread, turn_id=turn_id)

    def store(self, connection: Connection, replies: ReplyWindow) -> None:
        if replies != self.replies:
            self.update(connection, where="seq=?", parameters=(self.seq,), replies=replies)

    def opening_references(self, connection: Connection) -> tuple[MessageReference, ...]:
        return tuple(sorted((row.reference for row in ExternalTurnInput.select(
            connection, thread=self.thread, turn_id=self.turn_id)), key=lambda reference: reference.seq))


@dataclass(frozen=True)
class ExternalTurnInput(ExternalTranscriptRows, TypedTable):
    """An indexed opening edge, not a second copy in the turn's state payload."""

    thread: Annotated[ThreadLookup, Column(primary_key=True)]
    turn_id: Annotated[NativeTurnId, Column(primary_key=True)]
    reference: Annotated[MessageReference, Column(primary_key=True)]
    indexes = (Index(("thread", "reference")),)

    @classmethod
    def references(cls) -> tuple[ForeignKey, ...]:
        return (ForeignKey(columns=("thread", "turn_id"), target=ExternalTurn,
                           target_columns=("thread", "turn_id")),)


@dataclass(frozen=True)
class ExternalItemRow(ExternalTranscriptRows, ExternalPageRow, TypedTable):
    """A stable item position; older pages prepend without moving saved cursors."""

    unique = (("thread", "seq"),)
    indexes = (Index(("thread", "turn_id", "seq")),)

    def events_in(self, connection) -> tuple[TranscriptEvent, ...]:
        return tuple(row.event for row in ExternalEventRow.select(
            connection, thread=self.thread, seq=self.seq, order_by=("ordinal",)))

    def record(self, connection) -> ExternalRecord:
        return ExternalRecord(self.seq, self.events_in(connection))

    @classmethod
    def recent_turn_ids(
        cls, connection: Connection, thread: ThreadLookup
    ) -> Generator[NativeTurnId]:
        """Yield turn starts newest first, using the existing per-turn position index.

        A later item in an older turn must not move that turn ahead of a newer start:
        continuation windows compare first-event times, not latest activity.
        """
        table = f'"{cls.declared_name}"'
        first_item = (
            f'NOT EXISTS (SELECT 1 FROM {table} AS previous '
            f'WHERE previous.thread={table}.thread AND previous.turn_id={table}.turn_id '
            f'AND previous.seq<{table}.seq)'
        )
        with closing(cls.stream(connection, row_type=ExternalPageRow, thread=thread,
                                where=first_item, order_by=("seq",), descending=True)) as rows:
            for row in rows:
                yield row.turn_id


@dataclass(frozen=True)
class ExternalEventRow(ExternalTranscriptRows, TypedTable):
    """Each saved event exists once; SQL derives its query keys from that event."""

    thread: Annotated[ThreadLookup, Column(primary_key=True)]
    seq: Annotated[int, Column(primary_key=True)]
    ordinal: Annotated[int, Column(primary_key=True)]
    event: TranscriptEvent
    timestamp: ClassVar[Generated] = Generated(
        EventTime,
        Column(
            storage=RealStorage, nullable=True, generated="json_extract({event}, '$.timestamp')"
        ),
    )
    kind: ClassVar[Generated] = Generated(str, Column(generated="json_extract({event}, '$.kind')"))
    tool: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract({event}, '$.tool.tool')", nullable=True),
    )
    call_id: ClassVar[Generated] = Generated(
        str,
        Column(generated="json_extract({event}, '$.tool_call_id')", nullable=True),
    )

    foreign_keys = (
        ForeignKey(columns=("thread", "seq"), target=ExternalItemRow, target_columns=("thread", "seq"), on_delete="CASCADE"),
    )
    indexes = (
        Index(("thread", "kind", "tool", "timestamp", "call_id")),
        Index(("thread", "kind", "timestamp")),
        Index(("thread", "kind", "seq", "ordinal")),
    )

    @classmethod
    def append(cls, connection, thread, seq, events) -> None:
        for ordinal, event in enumerate(events):
            cls(thread, seq, ordinal, event).insert(connection)

    @classmethod
    def unjudged_query(cls, thread, todo, since):
        tools = tuple(tool.declared_name for tool in ToolDeclaration.members_with(MutatingPathTool))
        judged = todo.judged_calls
        placeholders = lambda values: ",".join("?" for _ in values)
        where = (f'e.thread=? AND e.kind=? AND e.tool IN ({placeholders(tools)}) '
                 'AND (e.timestamp>=? OR e.timestamp IS NULL)')
        parameters = (thread, ToolStartTranscript.declared_name, *tools, since)
        if judged:
            where += f' AND e.call_id NOT IN ({placeholders(judged)})'
            parameters += judged
        return where, parameters


@dataclass(frozen=True)
class ExternalRecord(TranscriptRecord):
    """An item at its page position, with events but no decoded native entry."""

    seq: int
    events: tuple[TranscriptEvent, ...]

    @property
    def start(self) -> int:
        return self.seq - 1

    @property
    def end(self) -> int:
        return self.seq

    @property
    def size(self) -> int:
        return sum(event.text_size for event in self.events)


@dataclass(frozen=True)
class ExternalCut(RecordsCut, TypedRow):
    """The thread's newest finished item sequence when read."""

    offset: Annotated[int, Column(generated="coalesce(max({seq}), 0)")]

    def contains(self, current: ExternalCut, through: int) -> bool:
        # Finished items are append-only: later items belong to the next update.
        return current.offset >= self.offset

    def draft_current(self, current: RecordsCut) -> bool:
        # Finishing a CLI item retires its draft atomically. Its current draft
        # must not be paired with a page captured before that completion.
        return self == current
