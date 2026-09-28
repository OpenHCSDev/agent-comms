"""Bounded transcript history and routing store ownership."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Generator, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .registration import Registration

if TYPE_CHECKING:
    pass
from .channel_targets import is_channel_target
from .errors import RelationViolationError
from .message_bus import MessageBus
from .messages import Message
from .messaging import Messaging
from .routing import MessageRoute, TurnRouting
from .threads import Thread
from .tool_results import ToolDiff
from .transcript_routes import InputDisplay, TranscriptRoutes

_LOG = logging.getLogger(__name__)


def _reverse_lines(path: Path, *, max_bytes: int | None = None) -> Iterator[bytes]:
    """Read JSONL newest-first without allocating the file or oversized lines."""
    try:
        with path.open("rb") as stream:
            position = stream.seek(0, 2)
            floor = max(0, position - max_bytes) if max_bytes is not None else 0
            pending = b""
            oversized = False
            while position > floor:
                count = min(65536, position - floor)
                position -= count
                stream.seek(position)
                parts = (stream.read(count) + pending).split(b"\n")
                pending = parts.pop(0)
                for line in reversed(parts):
                    if oversized:
                        oversized = False
                        continue
                    if line:
                        yield line
                if len(pending) > 256 * 1024:
                    pending = b""
                    oversized = True
            if floor == 0 and pending and not oversized:
                yield pending
    except OSError:
        return


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """One normalized event from a thread's persisted Pi transcript."""

    kind: str
    text: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    raw_input: object | None = None
    ok: bool = True
    routing: TurnRouting | None = None
    diff: ToolDiff | None = None

    @classmethod
    def from_wire(cls, data: Mapping) -> TranscriptEvent:
        return cls(
            **{
                **data,
                "routing": TurnRouting.from_wire(data["routing"]) if data.get("routing") else None,
                "diff": ToolDiff(**data["diff"]) if data.get("diff") else None,
            }
        )

    def to_wire(self, *, include_diff: bool = True) -> dict[str, object]:
        data = {**asdict(self), "routing": self.routing.to_wire() if self.routing else None}
        if self.diff is None or not include_diff:
            data.pop("diff", None)
        return data


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
            item.name: asdict(value) if isinstance(value, TranscriptCursor) else value
            for item in fields(self)
            if item.name != "events"
            for value in (getattr(self, item.name),)
        }


def _reverse_records(path: Path, before: int) -> Generator[tuple[int, int, bytes], None, None]:
    """Seek backwards in chunks; never parse or allocate the preceding history."""
    with path.open("rb") as stream:
        position = before
        end = before
        pending = b""
        while position:
            count = min(position, 65536)
            position -= count
            stream.seek(position)
            pending = stream.read(count) + pending
            while (boundary := pending.rfind(b"\n", 0, len(pending) - 1)) >= 0:
                start = position + boundary + 1
                yield start, end, pending[boundary + 1 :]
                pending, end = pending[: boundary + 1], start
        if pending:
            yield 0, end, pending


class Transcripts:
    def __init__(self, root: Path, registry: Registration, bus: MessageBus, messaging: Messaging):
        self.root = root
        self.registry = registry
        self.bus = bus
        self.messaging = messaging
        self.routes = TranscriptRoutes(root / "transcript_routes.json")

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
            for raw_line in _reverse_lines(path, max_bytes=max_bytes) if session_file else ():
                try:
                    payload = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                message = payload.get("message")
                events = self._transcript_record_events(
                    payload,
                    routes.get(payload.get("id", "")),
                    (
                        routes.input_display(message.get("inputId"))
                        if isinstance(message, Mapping)
                        else None
                    ),
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
                TranscriptEvent(
                    "notice",
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
            TranscriptEvent("notice", f"Forked from @{thread.parent}. This thread started with:"),
            *((TranscriptEvent("user", thread.task),) if thread.task else ()),
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
            routes_owner = TranscriptRoutes(Path(source.root) / "transcript_routes.json")
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

        def forward() -> Generator[tuple[int, int, bytes], None, None]:
            with path.open("rb") as stream:
                stream.seek(start)
                while True:
                    offset = stream.tell()
                    if offset >= size:
                        return
                    raw = stream.readline()
                    if not raw:
                        return
                    yield offset, stream.tell(), raw

        with routes:
            if size:
                iterator = forward() if after else _reverse_records(path, start)
                try:
                    for record_start, record_end, raw in iterator:
                        try:
                            value = json.loads(raw)
                        except (ValueError, UnicodeDecodeError):
                            # A writer may have left an incomplete last line. Retry it
                            # after the next append rather than losing its cursor.
                            if after and record_end == size and not raw.endswith(b"\n"):
                                break
                            events: tuple[TranscriptEvent, ...] = ()
                        else:
                            events = (
                                tuple(
                                    self._transcript_record_events(
                                        value,
                                        routes.get(value.get("id", "")),
                                        (
                                            routes.input_display(value["message"].get("inputId"))
                                            if isinstance(value.get("message"), Mapping)
                                            else None
                                        ),
                                    )
                                )
                                if isinstance(value, dict)
                                else ()
                            )
                        if (
                            events
                            and records
                            and (len(records) >= max_messages or used + len(raw) > max_bytes)
                        ):
                            break
                        if after:
                            end = record_end
                        else:
                            start = record_start
                        if events:
                            records.append(events)
                            used += len(raw)
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

    def _transcript_record_events(
        self,
        payload: Mapping[str, object],
        routing: TurnRouting | None = None,
        input_display: InputDisplay | None = None,
    ) -> list[TranscriptEvent]:
        if payload.get("type") == "compaction":
            summary = str(payload.get("summary") or "").strip()
            if summary:
                return [TranscriptEvent("notice", f"## Context compacted\n\n{summary}")]
            return []
        message = payload.get("message")
        if payload.get("type") != "message" or not isinstance(message, Mapping):
            return []
        return self._transcript_message_events(message, routing, input_display)

    def _transcript_message_events(
        self,
        message: Mapping[str, object],
        routing: TurnRouting | None = None,
        input_display: InputDisplay | None = None,
    ) -> list[TranscriptEvent]:
        role = message.get("role")
        if (
            role == "user"
            and input_display is not None
            and input_display.sent_text_digest is not None
        ):
            raw_content = message.get("content")
            raw_text = (
                raw_content
                if isinstance(raw_content, str)
                else (
                    "\n".join(
                        str(part.get("text") or "")
                        for part in raw_content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                    if isinstance(raw_content, list)
                    else None
                )
            )
            if raw_text is not None and input_display.matches(raw_text):
                # Exact per-input provenance wins over old turn-wide annotations,
                # including explicitly bound human/internal inputs (no route).
                routing = input_display.routing
            else:
                routing = None
                input_display = None
        if role == "user" and routing is not None and routing.requests:
            return [
                TranscriptEvent("user", request.body, routing=TurnRouting((request,), None))
                for request in routing.requests
            ]
        if role == "assistant" and message.get("stopReason") in {"error", "aborted"}:
            failure = str(message.get("errorMessage") or "").strip()
            if failure:
                return [TranscriptEvent("notice", f"[agent error] {failure}")]
        content = message.get("content")
        if isinstance(content, str):
            parts: Sequence[object] = ({"type": "text", "text": content},)
        elif isinstance(content, list):
            parts = content
        else:
            return []

        context_events: list[TranscriptEvent] = []
        if role == "user" and input_display is not None:
            raw_text = "\n".join(
                str(part.get("text") or "")
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if raw_text and raw_text != input_display.text:
                context_events.append(TranscriptEvent("context", raw_text))
            if input_display.text is None:
                return context_events
            # The owner records the user's original text before adding model-only
            # instructions. Preserve attachments while replacing just that text.
            parts = (
                {"type": "text", "text": input_display.text},
                *(part for part in parts if isinstance(part, dict) and part.get("type") != "text"),
            )

        events: list[TranscriptEvent] = context_events
        for part in parts:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if role == "user" and kind == "text":
                events.append(TranscriptEvent("user", str(part.get("text") or "")))
            elif role == "user" and kind == "image":
                events.append(
                    TranscriptEvent("user", f"[Image attachment: {part.get('mimeType', 'image')}]")
                )
            elif role == "assistant" and kind == "thinking":
                events.append(TranscriptEvent("thinking", str(part.get("thinking") or "")))
            elif role == "assistant" and kind == "text":
                text = str(part.get("text") or "")
                if events and events[-1].kind == "assistant":
                    events[-1] = replace(events[-1], text=events[-1].text + text)
                else:
                    events.append(TranscriptEvent("assistant", text))
            elif role == "assistant" and kind == "toolCall":
                events.append(
                    TranscriptEvent(
                        "tool_start",
                        tool_call_id=str(part.get("id") or ""),
                        tool_name=str(part.get("name") or "tool"),
                        raw_input=part.get("arguments"),
                    )
                )
            elif role == "toolResult":
                output = "\n".join(
                    str(item.get("text") or "")
                    for item in parts
                    if isinstance(item, dict) and item.get("type") == "text"
                )
                result = [
                    TranscriptEvent(
                        "tool_end",
                        text=output,
                        tool_call_id=str(message.get("toolCallId") or ""),
                        tool_name=str(message.get("toolName") or "tool"),
                        ok=not bool(message.get("isError")),
                        diff=ToolDiff.from_result(
                            str(message.get("toolName") or "tool"),
                            message,
                            not bool(message.get("isError")),
                        ),
                    )
                ]
                sent = self.messaging.sent_tool_message(
                    str(message.get("toolName") or ""), output, not bool(message.get("isError"))
                )
                if sent is not None:
                    result.append(
                        TranscriptEvent(
                            "sent",
                            sent.body,
                            routing=TurnRouting(reply=MessageRoute(sent.sender, (sent.target,))),
                        )
                    )
                return result
        return [replace(event, routing=routing) for event in events]

    def transcript_checkpoint(self, name: str) -> TranscriptCursor:
        session_file = self.registry.require(name).session_file or ""
        path = Path(session_file)
        return TranscriptCursor(
            session_file, path.stat().st_size if session_file and path.is_file() else 0
        )

    def record_input_display(
        self,
        native_id: str,
        display_text: str | None,
        *,
        sent_text: str | None = None,
        routing: TurnRouting | None = None,
    ) -> None:
        """Bind UI text to the private native input ID, never a prompt prefix."""
        self.routes.record_input_display(
            native_id, display_text, sent_text=sent_text, routing=routing
        )

    def record_turn_routing(
        self, name: str, checkpoint: TranscriptCursor, routing: TurnRouting
    ) -> None:
        session_file = self.registry.require(name).session_file
        if not session_file or not Path(session_file).is_file():
            return
        ids: list[str] = []
        if session_file == checkpoint.session_file:
            with Path(session_file).open("rb") as stream:
                stream.seek(checkpoint.offset)
                for raw in stream:
                    record = json.loads(raw)
                    if record.get("type") == "message" and isinstance(record.get("id"), str):
                        ids.append(record["id"])
        else:
            # A first turn or fork may create a new Pi file. Only annotate the
            # completed final assistant entry, never inherited entries by guess.
            for raw in _reverse_lines(Path(session_file)):
                record = json.loads(raw)
                if (
                    record.get("type") == "message"
                    and record.get("message", {}).get("role") == "assistant"
                ):
                    if isinstance(record.get("id"), str):
                        ids.append(record["id"])
                    break
        self.routes.record(session_file, tuple(ids), routing)

    def repair_input_routing(self, *, dry_run: bool = True) -> dict[str, int | bool]:
        """Explicit maintenance for old receipt-bound inputs, never a UI/wake scan.

        Join committed envelopes by sequence to owner-persisted native ID/text
        bindings. A prompt prefix or a matching body alone is not evidence.
        No transcript, input disposition, delivery/read cursor, or model is changed.
        """
        from .input_attempt import InputAttempt
        from .input_disposition import InputDispositions
        from .routing import ScheduledTurn

        rows = InputDispositions(self.root / InputDispositions.filename).read().bound_bus_inputs()
        existing = self.routes.input_bindings()
        groups: dict[str, list[InputAttempt]] = {}
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
            binding = (
                hashlib.sha256(sent_text.encode("utf-8")).hexdigest(),
                json.dumps(routing.to_wire(), sort_keys=True),
            )
            if native_id in existing:
                report["already_bound" if existing[native_id] == binding else "conflicts"] += 1
                continue
            report["eligible"] += 1
            if not dry_run:
                try:
                    self.record_input_display(
                        native_id, source, sent_text=sent_text, routing=routing
                    )
                except RelationViolationError:
                    report["conflicts"] += 1
                else:
                    report["repaired"] += 1
        return report
