"""Core's native activity selector; timestamps remain in the original session record."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .bus_projection import AppendCheckpoint, AppendProjectionStore
from .native_transcript import NativeRecord, reverse_records
from .private_path import FileRevision
from .recorded_time import RecordedEventTime


@dataclass(frozen=True)
class NativeToolCheckpoint(AppendCheckpoint):
    start: int
    call_id: str


class NativeToolActivity(AppendProjectionStore[NativeToolCheckpoint]):
    """One newest-call offset, published by the runtime's existing activity owner.

    The producer locates its just-announced call inside the current input only.
    Commander reads seek directly to that indexed record: no SDK index, history
    crawl, invented timestamp, or second copy of the record's timestamp.
    """
    record_type = NativeToolCheckpoint

    def __init__(self, root, incarnation, source: Path):
        super().__init__(source, root / f"native_tool_activity_{incarnation.require_recorded().lookup}.json")

    @staticmethod
    def contains(record: NativeRecord, call_id: str) -> bool:
        return record.entry is not None and any(
            call.id == call_id for message in record.entry.messages for call in message.tool_calls
        )

    def record(self, call_id: str) -> None:
        with self.source_path.open("rb") as stream:
            revision = FileRevision.from_stat(os.fstat(stream.fileno()))
            for start, end, raw in reverse_records(self.source_path, revision.size, stream=stream):
                record = NativeRecord.read(start, end, raw)
                if self.contains(record, call_id):
                    checkpoint = NativeToolCheckpoint(
                        revision, end, AppendCheckpoint.fingerprint(stream, end), start, call_id
                    )
                    break
                if record.entry is not None and record.entry.input_boundary:
                    raise ValueError(f"Announced native tool call {call_id} is absent from its input")
            else:
                raise ValueError(f"Announced native tool call {call_id} has no saved record")
        self.write(checkpoint)

    def last_tool_call(self):
        with self.source_path.open("rb") as stream:
            revision = FileRevision.from_stat(os.fstat(stream.fileno()))
            checkpoint = self.read().validated(revision, stream)
            if not isinstance(checkpoint, NativeToolCheckpoint):
                raise ValueError("Native tool activity has no valid producer index")
            stream.seek(checkpoint.start)
            record = NativeRecord.read(
                checkpoint.start, checkpoint.offset,
                stream.read(checkpoint.offset - checkpoint.start),
            )
            if not self.contains(record, checkpoint.call_id):
                raise ValueError("Native tool activity index names another record")
            return RecordedEventTime(record.entry.timestamp.at.timestamp())
