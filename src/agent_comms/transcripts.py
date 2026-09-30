"""Bounded transcript history and routing store ownership."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path

from .channel_targets import is_channel_target
from .errors import RelationViolationError
from .field_codec import FieldCodec, projected
from .message_bus import MessageBus
from .messages import Message
from .messaging import Messaging
from .native_entries import TranscriptProjection
from .native_transcript import NativeTranscript
from .native_runtime_input import NativeRuntimeInput, PublishedReplyRevision
from .bus_publication import CommittedDelivery, stable_thread_lookup
from .registration import Registration
from .routing import TurnRouting
from .threads import Thread
from .transcript_events import NoticeTranscript, TranscriptEvent, UserTranscript
from .transcript_routes import TranscriptRoutes, TranscriptRouteRevision
from .transcript_receipts import AssignedSourceCursor, AssignedSourceIdentity
from .store_files import file_revision
from .coordination_errors import StaleRevision

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranscriptCursor:
    session_file: str
    offset: int
    receipts: AssignedSourceCursor | None = None

    @property
    def wire_seq(self) -> int:
        return self.receipts.sequence if self.receipts is not None else 0

    def at_offset(self, offset: int) -> TranscriptCursor:
        from dataclasses import replace

        return replace(self, offset=offset)

    def at_sequence(self, sequence: int) -> TranscriptCursor:
        from dataclasses import replace

        if self.receipts is None:
            if sequence == 0:
                return self
            raise ValueError("Native-only cursor cannot advance an assigned wire source")
        return replace(self, receipts=self.receipts.at(sequence))

    def contains(self, other: TranscriptCursor) -> bool:
        return (
            self.session_file == other.session_file
            and self.offset >= other.offset
            and other.receipts_within(self)
        )

    def receipts_within(self, parent: TranscriptCursor) -> bool:
        return self.receipts is None or parent.includes_receipts(self.receipts)

    def includes_receipts(self, receipts: AssignedSourceCursor) -> bool:
        return self.receipts is not None and self.receipts.contains(receipts)

    def require_source(self, identity: AssignedSourceIdentity) -> None:
        if self.receipts is not None:
            self.receipts.require_source(identity)

    def covers_incoming(self, sequence: int) -> bool:
        # Callers hold a canonical recipient assignment, never an arbitrary seq.
        return self.receipts is not None and self.receipts.covers(sequence)


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    events: tuple[TranscriptEvent, ...]
    before: TranscriptCursor
    after: TranscriptCursor
    has_older: bool
    has_newer: bool

    def metadata(self) -> dict[str, object]:
        return {
            field.name: FieldCodec.encode(getattr(self, field.name))
            for field in fields(self)
            if field.name != "events"
        }


@dataclass(frozen=True, slots=True)
class TranscriptReadIdentity:
    """All canonical inputs to a bounded native page, including annotations."""

    root: str
    requested_name: str
    thread: Thread = field(metadata={"content_exclude": True})
    session_file: str
    native_revision: tuple[int, int, int, int] | None
    route_revision: TranscriptRouteRevision
    bus_revision: tuple[int, int, int, int] | None = field(metadata={"content_exclude": True})
    coordination_revision: tuple[int, int, int, int] | None = field(
        metadata={"content_exclude": True}
    )
    coordination_journal_revision: tuple[int, int, int, int] | None = field(
        metadata={"content_exclude": True}
    )
    reply_revision: PublishedReplyRevision
    read_revision: tuple[int, int, int, int] | None = field(metadata={"content_exclude": True})
    receipt_frontier: AssignedSourceCursor
    before: TranscriptCursor | None
    after: TranscriptCursor | None
    through: TranscriptCursor | None

    @projected(view="content", name="thread")
    def content_thread(self):
        return self.thread.incarnation, self.thread.parent, self.thread.task

    @projected(view="content", name="bus")
    def content_bus(self):
        # The certified frozen membership frontier owns relevant appends.
        # Keep inode custody: replacing the source is never an unrelated append.
        return self.bus_revision[0] if self.bus_revision is not None else None

    @property
    def content_identity(self) -> bytes:
        """Hashable source-owned inputs for existing preparation resources."""
        return json.dumps(
            FieldCodec.project(self, "content"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def same_content(self, other: TranscriptReadIdentity) -> bool:
        """Fence content, including the original native publication proof.

        Reader acknowledgements and thread activity do not change page content.
        The scoped published relation remains fenced: an obligation can commit
        just after its wire append and replace the native final reply projection.
        """
        return self.content_identity == other.content_identity


@dataclass(frozen=True, slots=True)
class TranscriptRead:
    """Deferred canonical read, fenced to the identity captured by its owner."""

    owner: Transcripts
    identity: TranscriptReadIdentity

    def current(self) -> bool:
        return self.identity == self.current_identity()

    def content_current(self) -> bool:
        return self.identity.same_content(self.current_identity())

    def current_identity(self) -> TranscriptReadIdentity:
        identity = self.identity
        return self.owner.capture_page_read(
            identity.requested_name,
            before=identity.before,
            after=identity.after,
            through=identity.through,
        ).identity

    def read(self) -> TranscriptPage:
        if not self.content_current():
            raise StaleRevision("Transcript read inputs changed before preparation")
        identity = self.identity
        page = self.owner.thread_transcript_page(
            identity.requested_name,
            before=identity.before,
            after=identity.after,
            through=identity.through,
        )
        if not self.content_current():
            raise StaleRevision("Transcript read inputs changed during preparation")
        return page


class Transcripts:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, messaging: Messaging):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.messaging = messaging
        self.routes = TranscriptRoutes(root)
        self.page_reads = 0

    def bind_page_read(
        self,
        name: str,
        identity: TranscriptReadIdentity,
        *,
        before: TranscriptCursor | None = None,
        after: TranscriptCursor | None = None,
        through: TranscriptCursor | None = None,
    ) -> TranscriptRead:
        """Bind the original published witness to this actual source request."""
        if identity.root != str(self.root):
            raise StaleRevision("Transcript witness belongs to another root")
        if (identity.before, identity.after, identity.through) != (before, after, through):
            raise StaleRevision("Transcript witness belongs to another page window")
        snapshot = self.registry.snapshot()
        try:
            requested = snapshot.owner_identity(name).incarnation
        except KeyError as error:
            raise StaleRevision("Transcript request has no current source") from error
        if identity.thread.incarnation.resolved(snapshot) != requested:
            raise StaleRevision("Transcript witness belongs to another original thread")
        return TranscriptRead(self, identity)

    def capture_page_read(
        self,
        name: str,
        *,
        before: TranscriptCursor | None = None,
        after: TranscriptCursor | None = None,
        through: TranscriptCursor | None = None,
    ) -> TranscriptRead:
        read, _sources = self.capture_page_window(
            name, before=before, after=after, through=through
        )
        return read

    def capture_page_window(
        self,
        name: str,
        *,
        source_limit: int = 1,
        before: TranscriptCursor | None = None,
        after: TranscriptCursor | None = None,
        through: TranscriptCursor | None = None,
    ) -> tuple[TranscriptRead, tuple[CommittedDelivery, ...]]:
        """Expose the source owner's complete identity without parsing its page.

        Native bytes alone are insufficient: input display/routing and older
        bus receipts contribute to the same projection. Registry-owned thread
        metadata also carries inherited fork text and source selection.
        """
        from .presentation import MessageNotification

        if not 1 <= source_limit <= MessageNotification.window_limit:
            raise ValueError("Transcript source capture requires a bounded window")
        thread, session_file, _ = self._thread_transcript_source(
            name,
            through.session_file if through is not None else None,
        )
        from .transcript_receipts import AssignedTranscriptSource

        receipt_frontier, sources = AssignedTranscriptSource.for_thread(
            self.root, thread, self.bus.log
        ).window(limit=source_limit)
        read = TranscriptRead(
            self,
            TranscriptReadIdentity(
                str(self.root),
                name,
                thread,
                session_file,
                file_revision(Path(session_file)) if session_file else None,
                self.routes.revision(),
                file_revision(self.bus.log.path),
                file_revision(self.root / "coordination.sqlite3"),
                file_revision(self.root / "coordination.sqlite3-wal"),
                (
                    NativeRuntimeInput.publication_revision(
                        self.root,
                        NativeTranscript(Path(session_file)),
                        stable_thread_lookup(thread.created_at),
                    )
                    if session_file and Path(session_file).is_file()
                    else PublishedReplyRevision(0, 0)
                ),
                file_revision(self.bus.reads.path),
                receipt_frontier,
                before,
                after,
                through,
            ),
        )
        return read, sources

    def thread_transcript(
        self, name: str, *, max_messages: int = 20, max_bytes: int = 64 * 1024
    ) -> Sequence[TranscriptEvent]:
        """The same canonical source supplies bounded preview and paged history."""
        if max_messages <= 0 or max_bytes <= 0:
            return ()
        page = self.thread_transcript_page(name, max_messages=max_messages, max_bytes=max_bytes)
        return (
            (NoticeTranscript("Earlier transcript content was omitted from this bounded view."),)
            if page.has_older
            else ()
        ) + page.events

    def _thread_transcript_source(
        self, name: str, source_file: str | None = None
    ) -> tuple[Thread, str, bool]:
        """Resolve a temporary inherited view without changing runtime ownership."""
        thread = self.registry.require(name)
        if thread.session_file and (source_file is None or source_file == thread.session_file):
            return thread, thread.session_file, False
        ancestor = thread
        visited = {thread.name}
        while ancestor.parent is not None:
            ancestor = self.registry.require(ancestor.parent)
            if ancestor.name in visited:
                break
            visited.add(ancestor.name)
            if ancestor.session_file and (
                source_file is None or source_file == ancestor.session_file
            ):
                return thread, ancestor.session_file, True
        if source_file:
            raise ValueError("Transcript changed; reload the latest page.")
        return thread, "", bool(thread.parent)

    @staticmethod
    def _fork_start_events(thread: Thread) -> tuple[TranscriptEvent, ...]:
        if not thread.parent:
            return ()
        return (
            NoticeTranscript(f"Forked from @{thread.parent}. This thread started with:"),
            *((UserTranscript(thread.task),) if thread.task else ()),
        )

    def thread_transcript_page(
        self,
        name: str,
        *,
        before: TranscriptCursor | None = None,
        after: TranscriptCursor | None = None,
        max_messages: int = 20,
        max_bytes: int = 64 * 1024,
        through: TranscriptCursor | None = None,
        historical_source: str | None = None,
    ) -> TranscriptPage:
        """Read one adjacent page with exclusive, file-bound byte cursors."""
        self.page_reads += 1
        if before is not None and after is not None:
            raise ValueError("Choose one transcript paging direction.")
        if max_messages <= 0 or max_bytes <= 0:
            raise ValueError("Transcript page budgets must be positive.")
        # A mounted inherited window remains pinned to its ancestor and byte
        # boundary when the child persists its own session. New unpinned reads
        # select the child's file; existing scroll cursors keep working.
        routes_owner = self.routes
        if historical_source is None:
            thread, session_file, inherited = self._thread_transcript_source(
                name, through.session_file if through is not None else None
            )
        else:
            source = next(
                (item for item in self.bus.history.sources() if item.key == historical_source), None
            )
            if source is None:
                raise ValueError("Historical source detached; refresh history")
            thread = source.registry().require(name)
            session_file, inherited = thread.session_file or "", False
            routes_owner = TranscriptRoutes(Path(source.root))
        cursor = before or after
        if cursor and (cursor.session_file != session_file or cursor.offset < 0):
            raise ValueError("Transcript changed; reload the latest page.")
        path = Path(session_file)
        size = path.stat().st_size if session_file and path.is_file() else 0
        if cursor and cursor.offset > size:
            raise ValueError("Transcript changed; reload the latest page.")
        if through is not None:
            if through.session_file != session_file or not 0 <= through.offset <= size:
                raise ValueError("Transcript changed; reload the latest page.")
            size = through.offset
            if cursor and cursor.offset > size:
                raise ValueError("Cursor is outside the transcript window.")
        from contextlib import closing
        from .transcript_receipts import (
            AssignedTranscriptSource,
            EarlierTranscript,
            LaterTranscript,
        )

        receipt_root = Path(source.root) if historical_source is not None else self.root
        from .wire_log import WireLog

        receipt_log = (
            WireLog(receipt_root / "bus.jsonl") if historical_source is not None else self.bus.log
        )
        receipts = AssignedTranscriptSource.for_thread(receipt_root, thread, receipt_log)
        frontier = through or TranscriptCursor(session_file, size, receipts.frontier)
        frontier.require_source(AssignedSourceIdentity(str(receipt_root), thread.incarnation))
        if frontier.wire_seq < 0 or (cursor is not None and not frontier.contains(cursor)):
            raise ValueError("Cursor is outside the combined transcript source")
        traversal = LaterTranscript() if after is not None else EarlierTranscript()
        initial = cursor or frontier
        consumed = initial
        records: list[tuple[TranscriptEvent, ...]] = []
        used = 0
        reader = NativeTranscript(path)
        native = traversal.native(reader, initial, frontier) if size else (record for record in ())
        from functools import partial

        with routes_owner.for_session(session_file) as routes:
            project_native = partial(receipts.native_events, routes=routes, reader=reader)
            try:
                record = next(native, None)
                events = record.project(project_native) if record is not None else ()
                source_rows = iter(
                    receipts.page_rows(
                        traversal, consumed.wire_seq, frontier, limit=max_messages + 1
                    )
                )
                message = next(source_rows, None)
                while record is not None or message is not None:
                    if record is not None and not events:
                        if after and record.incomplete_tail(size):
                            break
                        consumed = consumed.at_offset(traversal.native_position(record))
                        record = next(native, None)
                        events = record.project(project_native) if record is not None else ()
                        continue
                    # NativeEntry owns one clock for all parts of a record.
                    native_time = events[0].timestamp if events else None
                    choose_native = record is not None and (
                        message is None
                        or traversal.chooses_native(native_time, message.message.timestamp)
                    )
                    batch = events if choose_native else receipts.events(message)
                    cost = record.size if choose_native else sum(event.text_size for event in batch)
                    if records and (len(records) >= max_messages or used + cost > max_bytes):
                        break
                    records.append(batch)
                    used += cost
                    if choose_native:
                        consumed = consumed.at_offset(traversal.native_position(record))
                        record = next(native, None)
                        events = record.project(project_native) if record is not None else ()
                    else:
                        consumed = consumed.at_sequence(traversal.receipt_position(message.message))
                        message = next(source_rows, None)
                if record is None:
                    consumed = consumed.at_offset(frontier.offset if after else 0)
                if message is None:
                    consumed = consumed.at_sequence(frontier.wire_seq if after else 0)
            finally:
                native.close()
        start, end = traversal.bounds(initial, consumed)
        events = traversal.finish(records)
        if inherited and not after and (cursor is None or cursor == frontier):
            events = (*events, *self._fork_start_events(thread))
        return TranscriptPage(
            events, start, end, start.offset > 0 or start.wire_seq > 0, end != frontier
        )

    def transcript_checkpoint(self, name: str) -> TranscriptCursor:
        from .transcript_receipts import AssignedTranscriptSource

        thread = self.registry.require(name)
        session_file = thread.session_file or ""
        path = Path(session_file)
        return TranscriptCursor(
            session_file,
            path.stat().st_size if session_file and path.is_file() else 0,
            AssignedTranscriptSource.for_thread(self.root, thread, self.bus.log).frontier,
        )

    def record_turn_routing(
        self, name: str, checkpoint: TranscriptCursor, routing: TurnRouting
    ) -> None:
        session_file = self.registry.require(name).session_file
        if not session_file or not Path(session_file).is_file():
            return
        ids: list[str] = []
        reader = NativeTranscript(Path(session_file))
        if session_file == checkpoint.session_file:
            for record in reader.forward(checkpoint.offset, Path(session_file).stat().st_size):
                entry = record.entry
                if entry is not None and entry.is_message and entry.id is not None:
                    ids.append(entry.id)
        else:
            # A new/forked file annotates only the last assistant entry.
            for entry in reader.tail():
                if entry.assistant_message:
                    if entry.id is not None:
                        ids.append(entry.id)
                    break
        self.routes.record(session_file, tuple(ids), routing)

    def repair_input_routing(self, *, dry_run: bool = True) -> dict[str, int | bool]:
        """Explicit maintenance for old receipt-bound inputs, never a UI/wake scan.

        Join committed envelopes by sequence to owner-persisted native ID/text
        bindings. A prompt prefix or a matching body alone is not evidence.
        No transcript, input disposition, delivery/read cursor, or model is changed.
        """
        from .input_attempt import SentInput
        from .input_disposition import InputDispositions
        from .routing import ScheduledTurn

        rows = InputDispositions(self.root / InputDispositions.filename).read().bound_bus_inputs()
        existing = self.routes.input_bindings()
        groups: dict[str, list[SentInput]] = {}
        needed = {row.sequence for row in rows}
        envelopes: dict[int, Message] = {}
        aliases = self.registry.snapshot().aliases
        report: dict[str, int | bool] = {
            "dry_run": dry_run,
            "eligible": 0,
            "already_bound": 0,
            "repaired": 0,
            "skipped": 0,
            "conflicts": 0,
        }
        for row in rows:
            groups.setdefault(row.native_id, []).append(row)
        with self.bus.log.full_history_snapshot() as (_, messages):
            for message in messages:
                if message.seq in needed:
                    envelopes[message.seq] = message
        for native_id, group in groups.items():
            group.sort(key=lambda row: row.sequence)
            proof = {(row.owner, row.admission, row.turn_id, row.sent_text) for row in group}
            if (
                len(proof) != 1
                or len({row.sequence for row in group}) != len(group)
                or (len(group) > 1 and not all(is_channel_target(row.target) for row in group))
            ):
                report["conflicts"] += 1
                continue
            origins: list[Message] = []
            for row in group:
                candidate = envelopes.get(row.sequence)
                if (
                    candidate is None
                    or row.target != candidate.target
                    or row.source_text
                    not in {
                        ScheduledTurn.incoming(candidate).prompt,
                        ScheduledTurn.incoming(candidate, aliases=aliases).prompt,
                    }
                ):
                    break
                origins.append(candidate)
            source = "\n\n".join(row.source_text for row in group)
            sent_text = group[0].sent_text
            if len(origins) != len(group) or not sent_text.endswith(source):
                report["skipped"] += 1
                continue
            routing = TurnRouting(tuple(origins), None)
            if native_id in existing:
                binding = existing[native_id]
                matched = binding.matches(sent_text) and binding.routing == routing
                report["already_bound" if matched else "conflicts"] += 1
                continue
            report["eligible"] += 1
            if not dry_run:
                try:
                    self.routes.record_input_display(
                        native_id, source, sent_text=sent_text, routing=routing
                    )
                except RelationViolationError:
                    report["conflicts"] += 1
                else:
                    report["repaired"] += 1
        return report
