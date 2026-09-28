"""Bounded transcript history and routing store ownership."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path

from .channel_targets import is_channel_target
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .message_bus import MessageBus
from .messages import Message
from .messaging import Messaging
from .native_entries import TranscriptProjection
from .native_transcript import NativeTranscript
from .registration import Registration
from .routing import TurnRouting
from .threads import Thread
from .transcript_events import NoticeTranscript, TranscriptEvent, UserTranscript
from .transcript_routes import TranscriptRoutes

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TranscriptCursor:
    session_file: str
    offset: int


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


class Transcripts:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, messaging: Messaging):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.messaging = messaging
        self.routes = TranscriptRoutes(root)

    def thread_transcript(
        self,
        name: str,
        *,
        max_messages: int = 20,
        max_bytes: int = 64 * 1024,
    ) -> Sequence[TranscriptEvent]:
        """Return a bounded normalized tail of one thread's Pi session transcript."""
        thread, session_file, inherited = self._thread_transcript_source(name)
        if max_messages <= 0 or max_bytes <= 0:
            return ()
        path = Path(session_file)
        try:
            size = path.stat().st_size if session_file else 0
        except OSError:
            size = 0

        records: list[list[TranscriptEvent]] = []
        with self.routes.for_session(session_file) as routes:
            for entry in NativeTranscript(path).tail(max_bytes=max_bytes) if session_file else ():
                events = entry.events(
                    TranscriptProjection(
                        routes.get(entry.id),
                        routes.input_display(entry.input_id),
                        self.messaging.sent_tool_message,
                    )
                )
                if events:
                    records.append(events)
                    if len(records) > max_messages:
                        break

        truncated = size > max_bytes or len(records) > max_messages
        records = list(reversed(records[:max_messages]))
        events = [event for record in records for event in record]
        if truncated:
            events.insert(
                0,
                NoticeTranscript(
                    "Earlier transcript content was omitted from this bounded view.",
                ),
            )
        if inherited:
            events.extend(self._fork_start_events(thread))
        return tuple(events)

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
                (item for item in self.bus.history_sources() if item.key == historical_source), None
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
        start = end = cursor.offset if cursor else size
        records: list[tuple[TranscriptEvent, ...]] = []
        routes = routes_owner.for_session(session_file)
        used = 0

        with routes:
            if size:
                reader = NativeTranscript(path)
                iterator = reader.forward(start, size) if after else reader.reverse(start)
                try:
                    for record in iterator:
                        if (
                            after
                            and record.end == size
                            and not record.complete
                            and record.entry is None
                        ):
                            break  # Retry an incomplete writer tail after its next append.
                        entry = record.entry
                        events = (
                            tuple(
                                entry.events(
                                    TranscriptProjection(
                                        routes.get(entry.id),
                                        routes.input_display(entry.input_id),
                                        self.messaging.sent_tool_message,
                                    )
                                )
                            )
                            if entry is not None
                            else ()
                        )
                        if (
                            events
                            and records
                            and (len(records) >= max_messages or used + record.size > max_bytes)
                        ):
                            break
                        if after:
                            end = record.end
                        else:
                            start = record.start
                        if events:
                            records.append(events)
                            used += record.size
                finally:
                    iterator.close()
        if not after:
            records.reverse()
        events = tuple(event for record in records for event in record)
        if inherited and not after and (cursor is None or cursor.offset == size):
            events = (*events, *self._fork_start_events(thread))
        return TranscriptPage(
            events,
            TranscriptCursor(session_file, start),
            TranscriptCursor(session_file, end),
            start > 0,
            end < size,
        )

    def transcript_checkpoint(self, name: str) -> TranscriptCursor:
        session_file = self.registry.require(name).session_file or ""
        path = Path(session_file)
        return TranscriptCursor(
            session_file, path.stat().st_size if session_file and path.is_file() else 0
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
