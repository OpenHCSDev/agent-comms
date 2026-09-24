"""Bounded, atomic exports of the durable IRC-style message wire."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, ClassVar

from .bus_publication import reject_private_wire_fields
from .declarations import Message


class WireExportFormat(StrEnum):
    """Representations of committed wire envelopes."""

    JSONL = "jsonl"
    TEXT = "text"

    @property
    def importable(self) -> bool:
        return self is self.JSONL


class WireExportScopeKind(StrEnum):
    EVERYTHING = "everything"
    CHANNEL = "channel"
    DM = "dm"


@dataclass(frozen=True, slots=True)
class WireExportScope:
    """The authoritative conversation selected by the operations layer."""

    kind: WireExportScopeKind
    channel: str | None = None
    participants: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", WireExportScopeKind(self.kind))
        if self.kind is WireExportScopeKind.EVERYTHING:
            if self.channel is not None or self.participants:
                raise ValueError("The everything wire-export scope has no channel or participants.")
        elif self.kind is WireExportScopeKind.CHANNEL:
            if not self.channel or not self.channel.startswith("#") or self.participants:
                raise ValueError("A channel wire-export scope requires one #channel.")
            if self.channel == "#any":
                raise ValueError("#any is an aggregate projection, not an exportable conversation.")
        elif (
            self.channel is not None
            or len(self.participants) != 2
            or any(not participant for participant in self.participants)
            or self.participants[0] == self.participants[1]
        ):
            raise ValueError("A DM wire-export scope requires two distinct participants.")

    @classmethod
    def everything(cls) -> WireExportScope:
        return cls(WireExportScopeKind.EVERYTHING)

    @classmethod
    def for_channel(cls, channel: str) -> WireExportScope:
        return cls(WireExportScopeKind.CHANNEL, channel=channel)

    @classmethod
    def for_dm(cls, first: str, second: str) -> WireExportScope:
        return cls(WireExportScopeKind.DM, participants=(first, second))

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            **({"channel": self.channel} if self.channel is not None else {}),
            **({"participants": list(self.participants)} if self.participants else {}),
        }


class WireExportLimitKind(StrEnum):
    FULL = "full"
    MAX_BYTES = "max_bytes"
    RECENT = "recent"


@dataclass(frozen=True, slots=True)
class WireExportLimit:
    """One explicit, mutually exclusive wire-history bound."""

    kind: WireExportLimitKind
    value: int | float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", WireExportLimitKind(self.kind))
        if self.kind is WireExportLimitKind.FULL:
            if self.value is not None:
                raise ValueError("A full wire export has no bound value.")
        elif self.kind is WireExportLimitKind.MAX_BYTES:
            if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value <= 0:
                raise ValueError("The wire-export byte ceiling must be a positive integer.")
        elif (
            isinstance(self.value, bool)
            or not isinstance(self.value, (int, float))
            or not math.isfinite(self.value)
            or self.value < 0
        ):
            raise ValueError(
                "The recent wire-export cutoff must be a finite non-negative timestamp."
            )

    @classmethod
    def full(cls) -> WireExportLimit:
        return cls(WireExportLimitKind.FULL)

    @classmethod
    def max_bytes(cls, value: int) -> WireExportLimit:
        return cls(WireExportLimitKind.MAX_BYTES, value)

    @classmethod
    def recent(cls, cutoff: float) -> WireExportLimit:
        return cls(WireExportLimitKind.RECENT, cutoff)

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            **({"value": self.value} if self.value is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class WireExportBoundary:
    """One fixed wire snapshot boundary captured before export iteration."""

    through_seq: int
    export_started_at: float

    def __post_init__(self) -> None:
        if self.through_seq < 0:
            raise ValueError("The wire-export sequence boundary cannot be negative.")
        if not math.isfinite(self.export_started_at) or self.export_started_at < 0:
            raise ValueError("The wire-export start time must be finite and non-negative.")

    def to_wire(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WireExportReceipt:
    """Artifact provenance, never a delivery or source-session receipt."""

    destination: str
    format: WireExportFormat
    scope: WireExportScope
    limit: WireExportLimit
    boundary: WireExportBoundary
    source_messages: int
    exported_messages: int
    omitted_messages: int
    time_filtered_messages: int
    invalid_time_messages: int
    oversized_messages: int
    bytes_written: int
    output_sha256: str
    truncated: bool
    redacted_messages: int
    first_sequence: int | None
    last_sequence: int | None
    first_timestamp: float | None
    last_timestamp: float | None

    def to_wire(self) -> dict[str, object]:
        return {
            **asdict(self),
            "format": self.format.value,
            "scope": self.scope.to_wire(),
            "limit": self.limit.to_wire(),
            "boundary": self.boundary.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class _WireRow:
    message: Message
    data: bytes


class _ArtifactWriter:
    """Hashing byte writer over one private temporary file."""

    def __init__(self, output: BinaryIO) -> None:
        self.output = output
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> None:
        self.output.write(data)
        self.digest.update(data)
        self.size += len(data)


class WireTranscriptExporter:
    """Stream a fixed wire boundary into one private atomic artifact."""

    SCHEMA: ClassVar[str] = "agent-comms/wire-export"
    VERSION: ClassVar[int] = 1
    _TEXT_TIME_FORMAT: ClassVar[str] = "%Y-%m-%dT%H:%M:%S.%fZ"

    def __init__(
        self,
        *,
        format: WireExportFormat,
        scope: WireExportScope,
        limit: WireExportLimit,
        boundary: WireExportBoundary,
    ) -> None:
        self.format = WireExportFormat(format)
        self.scope = scope
        self.limit = limit
        self.boundary = boundary

    def export(
        self,
        envelopes: Iterable[Message | Mapping[str, object]],
        destination: Path,
        *,
        overwrite: bool = False,
    ) -> WireExportReceipt:
        """Consume ascending envelopes once and atomically publish the artifact."""
        header = self._header()
        if self.limit.kind is WireExportLimitKind.MAX_BYTES and len(header) > self._byte_ceiling():
            raise ValueError("The wire-export byte ceiling is too small for required metadata.")

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not overwrite and os.path.lexists(destination):
            raise FileExistsError(f"Wire-export destination {str(destination)!r} already exists.")

        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary_path = Path(temporary)
        source_messages = exported = time_filtered = invalid_time = oversized = 0
        first_selected: Message | None = None
        last_selected: Message | None = None
        previous_sequence = -1
        try:
            with os.fdopen(descriptor, "wb") as output:
                writer = _ArtifactWriter(output)
                if self.limit.kind is WireExportLimitKind.MAX_BYTES:
                    retained: deque[_WireRow] = deque()
                    retained_bytes = 0
                    for envelope in envelopes:
                        row = self._row(envelope)
                        previous_sequence = self._validate_sequence(row.message, previous_sequence)
                        if row.message.seq > self.boundary.through_seq:
                            break
                        source_messages += 1
                        row_size = len(row.data)
                        if len(header) + row_size > self._byte_ceiling():
                            retained.clear()
                            retained_bytes = 0
                            oversized += 1
                            continue
                        retained.append(row)
                        retained_bytes += row_size
                        while len(header) + retained_bytes > self._byte_ceiling():
                            retained_bytes -= len(retained.popleft().data)
                    writer.write(header)
                    for row in retained:
                        writer.write(row.data)
                    exported = len(retained)
                    if retained:
                        first_selected = retained[0].message
                        last_selected = retained[-1].message
                else:
                    writer.write(header)
                    cutoff = self._recent_cutoff()
                    for envelope in envelopes:
                        row = self._row(envelope)
                        previous_sequence = self._validate_sequence(row.message, previous_sequence)
                        if row.message.seq > self.boundary.through_seq:
                            break
                        source_messages += 1
                        if cutoff is not None:
                            if (
                                not math.isfinite(row.message.timestamp)
                                or row.message.timestamp <= 0
                            ):
                                invalid_time += 1
                                continue
                            if not (
                                cutoff <= row.message.timestamp <= self.boundary.export_started_at
                            ):
                                time_filtered += 1
                                continue
                        writer.write(row.data)
                        exported += 1
                        if first_selected is None:
                            first_selected = row.message
                        last_selected = row.message
                output.flush()
                os.fsync(output.fileno())
                bytes_written = writer.size
                output_sha256 = writer.digest.hexdigest()

            self._publish(temporary_path, destination, overwrite=overwrite)
        finally:
            temporary_path.unlink(missing_ok=True)

        omitted = source_messages - exported
        return WireExportReceipt(
            destination=str(destination),
            format=self.format,
            scope=self.scope,
            limit=self.limit,
            boundary=self.boundary,
            source_messages=source_messages,
            exported_messages=exported,
            omitted_messages=omitted,
            time_filtered_messages=time_filtered,
            invalid_time_messages=invalid_time,
            oversized_messages=oversized,
            bytes_written=bytes_written,
            output_sha256=output_sha256,
            truncated=omitted > 0,
            redacted_messages=0,
            first_sequence=first_selected.seq if first_selected else None,
            last_sequence=last_selected.seq if last_selected else None,
            first_timestamp=first_selected.timestamp if first_selected else None,
            last_timestamp=last_selected.timestamp if last_selected else None,
        )

    def _header(self) -> bytes:
        metadata = {
            "record": "header",
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "source": "agent-comms-wire",
            "format": self.format.value,
            "importable": self.format.importable,
            "scope": self.scope.to_wire(),
            "limit": self.limit.to_wire(),
            "boundary": self.boundary.to_wire(),
        }
        encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        if self.format is WireExportFormat.JSONL:
            return (encoded + "\n").encode()
        return (
            f"# agent-comms wire export v{self.VERSION} (non-importable text view)\n"
            f"# metadata: {encoded}\n"
        ).encode()

    def _row(self, envelope: Message | Mapping[str, object]) -> _WireRow:
        if isinstance(envelope, Message):
            message = envelope
            stored: Mapping[str, object] = message.to_wire()
        else:
            reject_private_wire_fields(envelope)
            stored = envelope
            message = Message.from_wire(stored)
        if self.format is WireExportFormat.JSONL:
            data = (
                json.dumps(
                    {"record": "message", "message": stored},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        else:
            timestamp = (
                datetime.fromtimestamp(message.timestamp, UTC).strftime(self._TEXT_TIME_FORMAT)
                if math.isfinite(message.timestamp) and message.timestamp > 0
                else "invalid-time"
            )
            attributes = [
                f"seq={message.seq}",
                f"id={message.message_id}",
                f"type={message.type.value}",
                f"role={message.sender_role.value}",
            ]
            if message.notice:
                attributes.append("notice=true")
            if message.membership is not None:
                attributes.append(f"membership={message.membership.value}")
            if message.mentions:
                attributes.append(
                    "mentions=" + ",".join(f"@{mention.thread}" for mention in message.mentions)
                )
            body = "\n".join(f"  | {line}" for line in message.body.split("\n"))
            data = (
                f"[{timestamp}] [{' '.join(attributes)}] "
                f"<{message.sender} -> {message.target}>\n{body}\n"
            ).encode()
        return _WireRow(message, data)

    @staticmethod
    def _validate_sequence(message: Message, previous: int) -> int:
        if message.seq <= previous:
            raise ValueError("Wire-export envelopes must have unique ascending sequences.")
        return message.seq

    def _byte_ceiling(self) -> int:
        assert self.limit.kind is WireExportLimitKind.MAX_BYTES
        assert isinstance(self.limit.value, int)
        return self.limit.value

    def _recent_cutoff(self) -> float | None:
        if self.limit.kind is not WireExportLimitKind.RECENT:
            return None
        assert isinstance(self.limit.value, (int, float))
        return float(self.limit.value)

    @staticmethod
    def _publish(temporary: Path, destination: Path, *, overwrite: bool) -> None:
        if overwrite:
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError:
                raise FileExistsError(
                    f"Wire-export destination {str(destination)!r} already exists."
                ) from None
        if os.name != "nt":
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
