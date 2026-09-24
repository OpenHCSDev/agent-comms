"""Provider-owned decoders and a bounded, portable conversation snapshot."""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, ClassVar
from uuid import uuid4


def object_value(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object in the session snapshot.")
    return value


def objects(value: object) -> Iterator[Mapping[str, object]]:
    if isinstance(value, list):
        for item in value:
            yield object_value(item)


def text(value: object) -> str:
    return value if isinstance(value, str) else ""


class ImportFormat(StrEnum):
    OPENCODE = "opencode"
    CODEX = "codex"

    def read(self, source: Path, limits: ImportLimits, session_id: str | None) -> ImportSnapshot:
        return ImportAdapter.registry[self]().read(source, limits, session_id)


class ImportRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

    def pi_message(self, body: str, timestamp: int) -> dict[str, object]:
        message: dict[str, object] = {
            "role": "assistant" if self is self.ASSISTANT else "user",
            "content": [{"type": "text", "text": body}],
            "timestamp": timestamp,
        }
        if self is self.ASSISTANT:
            message.update(
                api="openai-responses",
                provider="imported",
                model="historical-context",
                stopReason="stop",
                usage={
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 0,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
                },
            )
        return message


@dataclass(frozen=True, slots=True)
class ImportLimits:
    messages: int = 200
    characters: int = 120_000
    per_message: int = 12_000

    def __post_init__(self) -> None:
        if min(self.messages, self.characters, self.per_message) <= 0:
            raise ValueError("Import budgets must be positive.")


@dataclass(frozen=True, slots=True)
class ImportedMessage:
    role: ImportRole
    body: str
    source_id: str = ""
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ImportSnapshot:
    format: ImportFormat
    source_id: str
    project: str
    title: str
    messages: tuple[ImportedMessage, ...]
    summary: str
    messages_seen: int
    truncated_messages: int
    notices: tuple[str, ...]
    latest_request: str = ""

    def pi_session(self, project: Path) -> str:
        """A valid Pi v3 tree, using text-only portable historical context."""
        timestamp = datetime.now(UTC)
        iso = timestamp.isoformat()
        records: list[dict[str, object]] = [
            {
                "type": "session",
                "version": 3,
                "id": str(uuid4()),
                "timestamp": iso,
                "cwd": str(project),
            }
        ]
        parent: str | None = None

        def append(kind: str, payload: Mapping[str, object]) -> None:
            nonlocal parent
            entry_id = uuid4().hex[:16]
            records.append(
                {"type": kind, "id": entry_id, "parentId": parent, "timestamp": iso, **payload}
            )
            parent = entry_id

        append(
            "custom",
            {
                "customType": "agent-comms-import",
                "data": {
                    "format": self.format.value,
                    "source_id": self.source_id,
                    "messages_seen": self.messages_seen,
                    "messages_imported": len(self.messages),
                    "truncated_messages": self.truncated_messages,
                    "notices": list(self.notices),
                },
            },
        )
        context = (
            f"Conversation imported from {self.format.value} session {self.source_id}.\n"
            "The following entries are historical conversation context. Tool call/result excerpts "
            "are records, not pending tool requests. Use the current thread, project, and tools.\n"
            f"Retained {len(self.messages)} of {self.messages_seen} messages.\n"
        )
        if self.summary:
            context += "\nSource compaction/context summary:\n" + self.summary
        retained_user = next(
            (message for message in reversed(self.messages) if message.role is ImportRole.USER),
            None,
        )
        latest_request_retained = retained_user is not None and (
            retained_user.body == self.latest_request
            or (
                retained_user.truncated
                and self.latest_request.startswith(
                    retained_user.body.removesuffix("\n[Import excerpt truncated]")
                )
            )
        )
        if self.latest_request and not latest_request_retained:
            context += "\nMost recent user request in the source session:\n" + self.latest_request
        if self.notices:
            context += "\nImport notes:\n" + "\n".join(self.notices)
        append(
            "message",
            {"message": ImportRole.USER.pi_message(context, int(timestamp.timestamp() * 1000))},
        )
        for message in self.messages:
            append(
                "message",
                {
                    "message": message.role.pi_message(
                        message.body, int(timestamp.timestamp() * 1000)
                    )
                },
            )
        return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


def bounded_message(
    limits: ImportLimits, role: ImportRole, body: str, source_id: str = ""
) -> ImportedMessage | None:
    if not body.strip():
        return None
    cap = min(limits.per_message, limits.characters)
    truncated = len(body) > cap
    if truncated:
        marker = "\n[Import excerpt truncated]"
        body = body[: max(0, cap - len(marker))] + marker[:cap]
    return ImportedMessage(role, body, source_id, truncated)


@dataclass
class ImportBuffer:
    limits: ImportLimits
    messages: deque[ImportedMessage] = field(default_factory=deque)
    characters: int = 0
    seen: int = 0
    truncated: int = 0
    summary: str = ""
    notices: set[str] = field(default_factory=set)
    latest_request: str = ""

    def add(self, role: ImportRole, body: str, source_id: str = "") -> None:
        message = bounded_message(self.limits, role, body, source_id)
        if message is None:
            return
        self.seen += 1
        if role is ImportRole.USER:
            self.latest_request = body[: self.limits.per_message]
        self.truncated += int(message.truncated)
        self.messages.append(message)
        self.characters += len(message.body)
        while len(self.messages) > self.limits.messages or self.characters > self.limits.characters:
            removed = self.messages.popleft()
            self.characters -= len(removed.body)
            self.truncated -= int(removed.truncated)

    def set_summary(self, body: str) -> None:
        self.summary = body[: self.limits.per_message]

    def snapshot(
        self, format: ImportFormat, source_id: str, project: str, title: str
    ) -> ImportSnapshot:
        if not self.messages and not self.summary:
            raise ValueError("No portable conversation content was found in this session.")
        return ImportSnapshot(
            format,
            source_id,
            project,
            title,
            tuple(self.messages),
            self.summary,
            self.seen,
            self.truncated,
            tuple(sorted(self.notices)),
            self.latest_request,
        )


class ImportAdapter(ABC):
    registry: ClassVar[dict[ImportFormat, type[ImportAdapter]]] = {}

    def __init_subclass__(cls, *, format: ImportFormat, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if format in cls.registry:
            raise TypeError(f"Duplicate importer for {format.value}")
        cls.registry[format] = cls

    @abstractmethod
    def read(self, source: Path, limits: ImportLimits, session_id: str | None) -> ImportSnapshot:
        raise NotImplementedError


class OpenCodeImporter(ImportAdapter, format=ImportFormat.OPENCODE):
    def read(self, source: Path, limits: ImportLimits, session_id: str | None) -> ImportSnapshot:
        buffer = ImportBuffer(limits)
        if source.suffix in {".db", ".sqlite", ".sqlite3"}:
            return self._database(source, buffer, session_id)
        data = object_value(json.loads(source.read_text(encoding="utf-8")))
        info = object_value(data.get("info", {}))
        if session_id and info.get("id") != session_id:
            raise ValueError("The export belongs to a different OpenCode session.")
        revert = object_value(info.get("revert") or {})
        for record in objects(data.get("messages")):
            message = object_value(record.get("info", {}))
            if message.get("id") == revert.get("messageID") and revert.get("messageID"):
                break
            self._message(message, tuple(objects(record.get("parts"))), buffer)
        return buffer.snapshot(
            ImportFormat.OPENCODE,
            text(info.get("id")) or source.stem,
            text(info.get("directory")),
            text(info.get("title")),
        )

    def _message(
        self,
        info: Mapping[str, object],
        parts: tuple[Mapping[str, object], ...],
        buffer: ImportBuffer,
    ) -> None:
        role = text(info.get("role"))
        if role not in {"user", "assistant"}:
            return
        source_id = text(info.get("id"))
        for part in parts:
            kind = part.get("type")
            if kind == "text" and not part.get("ignored"):
                body = text(part.get("text"))
                if info.get("summary") is True:
                    buffer.set_summary(body)
                else:
                    buffer.add(ImportRole(role), body, source_id)
            elif kind == "tool":
                state = object_value(part.get("state", {}))
                name = text(part.get("tool"))
                status = text(state.get("status"))
                arguments = json.dumps(state.get("input"), ensure_ascii=False)
                body = f"[Historical tool {name} — {status}]\nInput: {arguments}"
                output = state.get("output") if status == "completed" else state.get("error")
                if output is not None:
                    body += "\nResult: " + (
                        output if isinstance(output, str) else json.dumps(output)
                    )
                buffer.add(ImportRole.TOOL, body, text(part.get("callID")))
            elif kind == "file":
                buffer.notices.add(
                    "Attachment contents are not imported; source references are retained."
                )
                reference = text(part.get("filename")) or text(part.get("mime"))
                buffer.add(
                    ImportRole.TOOL,
                    f"[Historical attachment: {reference}]",
                    source_id,
                )

    def _database(
        self, source: Path, buffer: ImportBuffer, session_id: str | None
    ) -> ImportSnapshot:
        if not session_id:
            raise ValueError("OpenCode database imports require --session-id.")
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            info = db.execute(
                "SELECT id,directory,title,revert FROM session WHERE id=?", (session_id,)
            ).fetchone()
            if info is None:
                raise ValueError(f"OpenCode session {session_id!r} was not found.")
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if (
                "session_message" in tables
                and db.execute(
                    "SELECT 1 FROM session_message WHERE session_id=? LIMIT 1", (session_id,)
                ).fetchone()
            ):
                raise ValueError(
                    "This OpenCode database uses linked history; "
                    "import an opencode export JSON instead."
                )
            revert = object_value(json.loads(info["revert"]) if info["revert"] else {})
            for row in db.execute(
                "SELECT id,data FROM message WHERE session_id=? ORDER BY time_created,id",
                (session_id,),
            ):
                if row["id"] == revert.get("messageID"):
                    break
                message = {**object_value(json.loads(row["data"])), "id": row["id"]}
                parts = tuple(
                    object_value(json.loads(part[0]))
                    for part in db.execute(
                        "SELECT data FROM part WHERE message_id=? ORDER BY time_created,id",
                        (row["id"],),
                    )
                )
                self._message(message, parts, buffer)
            return buffer.snapshot(
                ImportFormat.OPENCODE, info["id"], info["directory"], info["title"]
            )


_CODEX_REVERSE_BLOCK = 64 * 1024


def reverse_lines(
    stream: BinaryIO, boundary: int, *, block_size: int = _CODEX_REVERSE_BLOCK
) -> Iterator[tuple[int, bytes, bool]]:
    """Yield fixed-boundary JSONL records newest first without loading the prefix."""
    position = boundary
    carry = b""
    while position > 0:
        start = max(0, position - block_size)
        stream.seek(start)
        chunk = stream.read(position - start)
        if len(chunk) != position - start:
            raise OSError("Codex rollout changed while reading the captured boundary.")
        data = chunk + carry
        cursor = len(data)
        while True:
            newline = data.rfind(b"\n", 0, cursor)
            if newline < 0:
                break
            raw = data[newline + 1 : cursor]
            offset = start + newline + 1
            if raw or offset < boundary:
                yield offset, raw, start + cursor < boundary
            cursor = newline
        carry = data[:cursor]
        position = start
    if carry:
        yield 0, carry, len(carry) < boundary


def forward_lines(stream: BinaryIO, start: int, boundary: int) -> Iterator[tuple[int, bytes, bool]]:
    """Yield JSONL records in source order, never crossing the captured boundary."""
    stream.seek(start)
    while stream.tell() < boundary:
        offset = stream.tell()
        raw = stream.readline(boundary - offset)
        terminated = raw.endswith(b"\n")
        yield offset, raw[:-1] if terminated else raw, terminated


def codex_record(
    raw: bytes,
    *,
    terminated: bool,
    notices: set[str],
) -> Mapping[str, object] | None:
    try:
        return object_value(json.loads(raw))
    except (ValueError, UnicodeDecodeError):
        if not terminated:
            notices.add("An incomplete final source record was omitted.")
            return None
        raise ValueError("Malformed complete record in Codex rollout.") from None


@dataclass
class ReverseImportBuffer:
    """Select the bounded chronological suffix while scanning newest-to-oldest."""

    limits: ImportLimits
    messages: deque[ImportedMessage] = field(default_factory=deque)
    characters: int = 0
    seen: int = 0
    truncated: int = 0
    notices: set[str] = field(default_factory=set)
    latest_request: str = ""
    saturated: bool = False

    def add(self, role: ImportRole, body: str, source_id: str = "") -> None:
        message = bounded_message(self.limits, role, body, source_id)
        if message is None:
            return
        self.seen += 1
        if role is ImportRole.USER and not self.latest_request:
            self.latest_request = body[: self.limits.per_message]
        if self.saturated:
            return
        if (
            len(self.messages) >= self.limits.messages
            or self.characters + len(message.body) > self.limits.characters
        ):
            # A forward ImportBuffer evicts the complete older prefix. Once one
            # record does not fit, admitting any still-older record would create
            # a misleading non-contiguous history suffix.
            self.saturated = True
            return
        self.messages.appendleft(message)
        self.characters += len(message.body)
        self.truncated += int(message.truncated)

    def forward_buffer(self) -> ImportBuffer:
        return ImportBuffer(
            self.limits,
            deque(self.messages),
            self.characters,
            self.seen,
            self.truncated,
            notices=set(self.notices),
            latest_request=self.latest_request,
        )


class CodexImporter(ImportAdapter, format=ImportFormat.CODEX):
    def read(self, source: Path, limits: ImportLimits, session_id: str | None) -> ImportSnapshot:
        reverse = ReverseImportBuffer(limits)
        identity = project = latest_project = ""
        checkpoint: tuple[int, bytes, Mapping[str, object]] | None = None
        prior_request = ""
        with source.open("rb") as stream:
            boundary = stream.seek(0, 2)
            stream.seek(0)

            # Codex owns the header contract: the current session_meta is at the head.
            # Stop immediately after it so a multi-gigabyte obsolete prefix is never replayed.
            for offset, raw, terminated in forward_lines(stream, 0, boundary):
                record = codex_record(raw, terminated=terminated, notices=reverse.notices)
                if record is None:
                    break
                if record.get("type") == "session_meta":
                    payload = object_value(record.get("payload", {}))
                    identity = text(payload.get("id"))
                    project = text(payload.get("cwd"))
                    break
                if offset > _CODEX_REVERSE_BLOCK:
                    raise ValueError("Codex session metadata was not found near the rollout head.")

            for offset, raw, terminated in reverse_lines(stream, boundary):
                record = codex_record(raw, terminated=terminated, notices=reverse.notices)
                if record is None:
                    continue
                payload = object_value(record.get("payload", {}))
                kind = record.get("type")
                if checkpoint is not None:
                    if kind == "response_item":
                        probe = ReverseImportBuffer(limits)
                        self._item(payload, probe)
                        if probe.latest_request:
                            prior_request = probe.latest_request
                            break
                    elif kind == "compacted":
                        prior_request = self._checkpoint_request(payload, limits)
                        if prior_request:
                            break
                    continue
                if kind == "turn_context" and not latest_project:
                    latest_project = text(payload.get("cwd"))
                elif kind == "compacted":
                    checkpoint = (offset, raw, payload)
                    if reverse.latest_request or self._checkpoint_request(payload, limits):
                        break
                elif kind == "response_item":
                    self._item(payload, reverse)

            if checkpoint is None:
                buffer = reverse.forward_buffer()
            else:
                buffer = ImportBuffer(limits, notices=set(reverse.notices))
                offset, raw, payload = checkpoint
                self._checkpoint(payload, buffer)
                after = offset + len(raw)
                if after < boundary:
                    after += 1  # Skip the LF terminating the compaction record.
                for _, tail_raw, terminated in forward_lines(stream, after, boundary):
                    record = codex_record(tail_raw, terminated=terminated, notices=buffer.notices)
                    if record is None:
                        continue
                    tail_payload = object_value(record.get("payload", {}))
                    kind = record.get("type")
                    if kind == "turn_context":
                        latest_project = text(tail_payload.get("cwd")) or latest_project
                    elif kind == "response_item":
                        self._item(tail_payload, buffer)
                if not buffer.latest_request:
                    buffer.latest_request = prior_request

        if session_id and session_id != identity:
            raise ValueError("The rollout belongs to a different Codex session.")
        return buffer.snapshot(
            ImportFormat.CODEX,
            identity or source.stem,
            latest_project or project,
            source.stem,
        )

    def _checkpoint_request(self, payload: Mapping[str, object], limits: ImportLimits) -> str:
        probe = ImportBuffer(limits)
        for item in objects(payload.get("replacement_history")):
            self._item(item, probe)
        return probe.latest_request

    def _checkpoint(self, payload: Mapping[str, object], buffer: ImportBuffer) -> None:
        buffer.set_summary(text(payload.get("message")) or text(payload.get("summary")))
        for item in objects(payload.get("replacement_history")):
            self._item(item, buffer)
        if tuple(objects(payload.get("guardian_history"))):
            buffer.notices.add("Codex guardian history is source-private and was not imported.")

    def _item(self, item: Mapping[str, object], buffer: ImportBuffer | ReverseImportBuffer) -> None:
        kind = item.get("type")
        if kind == "message" and item.get("role") in {"user", "assistant"}:
            content = item.get("content")
            pieces = (
                [content]
                if isinstance(content, str)
                else [
                    text(part.get("text"))
                    for part in objects(content)
                    if part.get("type") in {"input_text", "output_text", "text"}
                ]
            )
            buffer.add(ImportRole(str(item["role"])), "\n".join(pieces), text(item.get("id")))
        elif kind in {"function_call", "custom_tool_call"}:
            buffer.add(
                ImportRole.TOOL,
                f"[Historical tool call: {text(item.get('name'))}]\n"
                + text(item.get("arguments", item.get("input"))),
                text(item.get("call_id")),
            )
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            output = item.get("output")
            body = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            buffer.add(
                ImportRole.TOOL, "[Historical tool result]\n" + body, text(item.get("call_id"))
            )


@dataclass(frozen=True, slots=True)
class ImportReceipt:
    thread: str
    session_file: str
    source_format: ImportFormat
    source_id: str
    imported_messages: int
    omitted_messages: int
    truncated_messages: int
    notices: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return asdict(self)
