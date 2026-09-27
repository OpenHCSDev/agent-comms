"""Bounded, atomic exports of the durable IRC-style message wire."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from abc import abstractmethod
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, cast

from .bus_publication import reject_private_wire_fields
from .channels import ChannelCatalog
from .declarations import BuiltinChannel, Message, RelationViolationError, ThreadRegistry
from .declared_family import DeclaredFamily, _FamilyMeta
from .field_codec import FieldCodec


class _FormatMeta(_FamilyMeta):
    """Keep the enum-era constructor, constants and CLI iteration at the boundary."""

    def __call__(self, *args: Any, **kwargs: Any) -> WireExportFormat:
        if self is WireExportFormat:
            return WireExportFormat.parse(*args, **kwargs)
        return cast(WireExportFormat, super().__call__(*args, **kwargs))

    def __iter__(self) -> Iterator[WireExportFormat]:
        return (member() for member in WireExportFormat.members_with(WireExportFormat))

    def __getattr__(self, name: str) -> WireExportFormat:
        if name.isupper():
            try:
                return WireExportFormat.decode(name.lower())()
            except ValueError:
                pass
        raise AttributeError(name)


class WireExportFormat(DeclaredFamily, metaclass=_FormatMeta, affix="Format"):
    """A representation owns both header and row rendering."""

    importable: ClassVar[bool] = False

    @classmethod
    def parse(cls, value: str | WireExportFormat) -> WireExportFormat:
        """Decode once at a string boundary; typed instances retain their identity."""
        return value if isinstance(value, WireExportFormat) else cls.decode(value)()

    @property
    def value(self) -> str:
        return self.declared_name

    def __str__(self) -> str:
        return self.value

    @abstractmethod
    def header(self, metadata: Mapping[str, object]) -> bytes:
        """Render the durable metadata record."""

    @abstractmethod
    def row(self, message: Message, stored: Mapping[str, object]) -> bytes:
        """Render a parsed message and its lossless stored envelope."""

    @staticmethod
    def json_record(record: Mapping[str, object]) -> str:
        return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class JsonlFormat(WireExportFormat):
    importable: ClassVar[bool] = True

    def header(self, metadata: Mapping[str, object]) -> bytes:
        return (self.json_record(metadata) + "\n").encode()

    def row(self, message: Message, stored: Mapping[str, object]) -> bytes:
        return (self.json_record({"record": "message", "message": stored}) + "\n").encode()


@dataclass(frozen=True)
class TextFormat(WireExportFormat):
    def header(self, metadata: Mapping[str, object]) -> bytes:
        return (
            f"# agent-comms wire export v{metadata['version']} (non-importable text view)\n"
            f"# metadata: {self.json_record(metadata)}\n"
        ).encode()

    def row(self, message: Message, stored: Mapping[str, object]) -> bytes:
        timestamp = (
            datetime.fromtimestamp(message.timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
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
            attributes.append("mentions=" + ",".join(f"@{m.thread}" for m in message.mentions))
        body = "\n".join(f"  | {line}" for line in message.body.split("\n"))
        return (
            f"[{timestamp}] [{' '.join(attributes)}] "
            f"<{message.sender} -> {message.target}>\n{body}\n"
        ).encode()


@dataclass(frozen=True)
class ResolvedExportScope:
    """One canonical scope and its alias/catalog-resolved predicate snapshot."""

    scope: WireExportScope
    matches: Callable[[Message], bool]


class WireExportScope(DeclaredFamily, affix="Scope"):
    """The authoritative conversation, resolved under the wire snapshot lock."""

    @staticmethod
    def everything() -> WireExportScope:
        return EverythingScope()

    @staticmethod
    def for_channel(channel: str) -> WireExportScope:
        return ChannelScope(channel)

    @staticmethod
    def for_dm(first: str, second: str) -> WireExportScope:
        return DmScope((first, second))

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode(self)

    @abstractmethod
    def resolve(self, catalog: ChannelCatalog, registry: ThreadRegistry) -> ResolvedExportScope:
        """Capture canonical names and a predicate without mutating read state."""


@dataclass(frozen=True, slots=True)
class EverythingScope(WireExportScope):
    def resolve(self, catalog: ChannelCatalog, registry: ThreadRegistry) -> ResolvedExportScope:
        return ResolvedExportScope(self, lambda message: True)


@dataclass(frozen=True, slots=True)
class ChannelScope(WireExportScope):
    channel: str

    def __post_init__(self) -> None:
        if not isinstance(self.channel, str) or not self.channel.startswith("#"):
            raise ValueError("A channel wire-export scope requires one #channel.")
        builtin = BuiltinChannel.lookup(self.channel)
        if builtin is not None and builtin.aggregate:
            raise ValueError(
                f"{self.channel} is an aggregate projection, not an exportable conversation."
            )

    def resolve(self, catalog: ChannelCatalog, registry: ThreadRegistry) -> ResolvedExportScope:
        if catalog.is_view_target(self.channel):
            raise RelationViolationError(
                f"Saved view {self.channel!r} has no authoritative wire history."
            )
        targets = catalog.history_targets(self.channel)
        if targets is None:
            raise RelationViolationError(
                f"View {self.channel!r} is an aggregate, not an exportable conversation."
            )
        return ResolvedExportScope(self, lambda message: message.target in targets)


@dataclass(frozen=True, slots=True)
class DmScope(WireExportScope):
    participants: tuple[str, str]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.participants, tuple)
            or len(self.participants) != 2
            or any(not isinstance(name, str) or not name for name in self.participants)
            or self.participants[0] == self.participants[1]
        ):
            raise ValueError("A DM wire-export scope requires two distinct participants.")

    def resolve(self, catalog: ChannelCatalog, registry: ThreadRegistry) -> ResolvedExportScope:
        first = registry.require(self.participants[0]).name
        second = registry.require(self.participants[1]).name
        canonical = DmScope((first, second))
        first_names = registry.aliases_for(first)
        second_names = registry.aliases_for(second)

        def matches(message: Message) -> bool:
            return (message.sender in first_names and message.target in second_names) or (
                message.sender in second_names and message.target in first_names
            )

        return ResolvedExportScope(canonical, matches)


@dataclass
class ExportSelectionStats:
    """Selection counters shared by the traversal and limit hooks."""

    source_messages: int = 0
    time_filtered: int = 0
    invalid_time: int = 0
    oversized: int = 0


class WireExportLimit(DeclaredFamily, affix="Limit"):
    """Shared filtering algorithm with declaration-owned retention hooks."""

    @staticmethod
    def full() -> WireExportLimit:
        return FullLimit()

    @staticmethod
    def max_bytes(value: int) -> WireExportLimit:
        return MaxBytesLimit(value)

    @staticmethod
    def recent(cutoff: float) -> WireExportLimit:
        return RecentLimit(cutoff)

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode(self)

    def byte_ceiling(self) -> int | None:
        return None

    def time_cutoff(self) -> float | None:
        return None

    def validate_header(self, size: int) -> None:
        ceiling = self.byte_ceiling()
        if ceiling is not None and size > ceiling:
            raise ValueError("The wire-export byte ceiling is too small for required metadata.")

    def select(
        self,
        rows: Iterable[WireExportRow],
        header_size: int,
        started_at: float,
        stats: ExportSelectionStats,
    ) -> Iterable[WireExportRow]:
        cutoff = self.time_cutoff()

        def filtered() -> Iterable[WireExportRow]:
            for row in rows:
                if cutoff is not None:
                    timestamp = row.message.timestamp
                    if not math.isfinite(timestamp) or timestamp <= 0:
                        stats.invalid_time += 1
                        continue
                    if not cutoff <= timestamp <= started_at:
                        stats.time_filtered += 1
                        continue
                yield row

        return self.retain(filtered(), header_size, stats)

    @abstractmethod
    def retain(
        self,
        rows: Iterable[WireExportRow],
        header_size: int,
        stats: ExportSelectionStats,
    ) -> Iterable[WireExportRow]:
        """Select an ascending stream; buffering is owned by the bound's declaration."""


@dataclass(frozen=True, slots=True)
class FullLimit(WireExportLimit):
    def retain(
        self,
        rows: Iterable[WireExportRow],
        header_size: int,
        stats: ExportSelectionStats,
    ) -> Iterable[WireExportRow]:
        return rows


@dataclass(frozen=True, slots=True)
class MaxBytesLimit(WireExportLimit):
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value <= 0:
            raise ValueError("The wire-export byte ceiling must be a positive integer.")

    def byte_ceiling(self) -> int:
        return self.value

    def retain(
        self,
        rows: Iterable[WireExportRow],
        header_size: int,
        stats: ExportSelectionStats,
    ) -> Iterable[WireExportRow]:
        retained: deque[WireExportRow] = deque()
        retained_bytes = 0
        for row in rows:
            row_size = len(row.data)
            if header_size + row_size > self.value:
                retained.clear()
                retained_bytes = 0
                stats.oversized += 1
                continue
            retained.append(row)
            retained_bytes += row_size
            while header_size + retained_bytes > self.value:
                retained_bytes -= len(retained.popleft().data)
        return retained


@dataclass(frozen=True, slots=True)
class RecentLimit(FullLimit):
    value: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, (int, float))
            or not math.isfinite(self.value)
            or self.value < 0
        ):
            raise ValueError(
                "The recent wire-export cutoff must be a finite non-negative timestamp."
            )

    def time_cutoff(self) -> float:
        return self.value


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
class WireExportRow:
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

    def __init__(
        self,
        *,
        format: WireExportFormat,
        scope: WireExportScope,
        limit: WireExportLimit,
        boundary: WireExportBoundary,
    ) -> None:
        self.format = WireExportFormat.parse(format)
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
        self.limit.validate_header(len(header))

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not overwrite and os.path.lexists(destination):
            raise FileExistsError(f"Wire-export destination {str(destination)!r} already exists.")

        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary_path = Path(temporary)
        stats = ExportSelectionStats()
        exported = 0
        first_selected: Message | None = None
        last_selected: Message | None = None
        try:
            with os.fdopen(descriptor, "wb") as output:
                writer = _ArtifactWriter(output)
                writer.write(header)
                rows = self._rows(envelopes, stats)
                for row in self.limit.select(
                    rows, len(header), self.boundary.export_started_at, stats
                ):
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

        omitted = stats.source_messages - exported
        return WireExportReceipt(
            destination=str(destination),
            format=self.format,
            scope=self.scope,
            limit=self.limit,
            boundary=self.boundary,
            source_messages=stats.source_messages,
            exported_messages=exported,
            omitted_messages=omitted,
            time_filtered_messages=stats.time_filtered,
            invalid_time_messages=stats.invalid_time,
            oversized_messages=stats.oversized,
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
        return self.format.header(metadata)

    def _rows(
        self,
        envelopes: Iterable[Message | Mapping[str, object]],
        stats: ExportSelectionStats,
    ) -> Iterable[WireExportRow]:
        previous_sequence = -1
        for envelope in envelopes:
            row = self._row(envelope)
            previous_sequence = self._validate_sequence(row.message, previous_sequence)
            if row.message.seq > self.boundary.through_seq:
                break
            stats.source_messages += 1
            yield row

    def _row(self, envelope: Message | Mapping[str, object]) -> WireExportRow:
        if isinstance(envelope, Message):
            message = envelope
            stored: Mapping[str, object] = message.to_wire()
        else:
            reject_private_wire_fields(envelope)
            stored = envelope
            message = Message.from_wire(stored)
        return WireExportRow(message, self.format.row(message, stored))

    @staticmethod
    def _validate_sequence(message: Message, previous: int) -> int:
        if message.seq <= previous:
            raise ValueError("Wire-export envelopes must have unique ascending sequences.")
        return message.seq

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
