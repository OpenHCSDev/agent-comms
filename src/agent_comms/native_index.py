"""Original native EntryStore selections, never a second Core history index."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .pi_helper import PiHelper, SessionHelperRequest
from .private_path import FileRevision


@dataclass(frozen=True)
class NativeEntryPosition:
    entry_id: str
    start: int
    end: int

    def read(self, stream, revision: FileRevision):
        from .native_transcript import NativeRecord

        if not 0 <= self.start < self.end <= revision.size:
            raise ValueError("Native index position is outside its original source")
        stream.seek(self.start)
        raw = stream.read(self.end - self.start)
        record = NativeRecord.read(self.start, self.end, raw)
        if (not record.complete or record.entry is None
                or record.entry.require_entry_id() != self.entry_id):
            raise ValueError("Native index does not select its original entry")
        return record


@dataclass(frozen=True)
class NativeIndexObservation:
    revision: FileRevision
    latest_tool: NativeEntryPosition | None

    def tool_activity(self, path: Path, stream):
        from .native_entries import TranscriptProjection
        from .tool_activity import NoToolActivity, RecordedToolActivity

        self.require_current(path, stream)
        if self.latest_tool is None:
            activity = NoToolActivity()
        else:
            record = self.latest_tool.read(stream, self.revision)
            calls = tuple(call for event in record.entry.events(TranscriptProjection())
                          for call in event.tool_calls)
            if not calls:
                raise ValueError("Native indexed tool entry contains no original call")
            activity = RecordedToolActivity(calls[-1])
        self.require_current(path, stream)
        return activity

    def require_current(self, path: Path, stream):
        if (FileRevision.from_file(stream) != self.revision
                or FileRevision.capture(path) != self.revision):
            raise ValueError("Native indexed source changed; reopen, never replay")


class NativeIndexHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "native_index.mjs"
    request = SessionHelperRequest
    result = NativeIndexObservation

    @classmethod
    def observe(cls, file: Path) -> NativeIndexObservation:
        from .native_arguments import NativeArguments
        from .native_pi import NativePiRpcLaunch

        file = file.absolute()
        package = NativePiRpcLaunch.package_for_command(NativeArguments.configured_binary())
        return asyncio.run(cls.run(
            SessionHelperRequest(str(package), str(file)), cwd=file.parent,
        ))
