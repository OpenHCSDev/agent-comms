from __future__ import annotations

from dataclasses import dataclass, field, replace

from agent_comms.agent_backend import RuntimeLocation
from agent_comms.execution.native_record_file import SessionCut
from agent_comms.execution.transcript_cursor_reads import MarksSourceRead, TranscriptRuntime
from agent_comms.execution.transcript_sources import (
    FileTranscriptCursor,
    NoWireFrontier,
    TranscriptCursor,
    WireFrontier,
)
from agent_comms.storage.private_path import FileIdentity, MissingFile


@dataclass(frozen=True, slots=True)
class NoNativeTranscriptCursor(FileTranscriptCursor):
    """A thread owns no native read mark, even while showing an inherited source."""

    source: RuntimeLocation = field(kw_only=True)
    offset: int = 0
    wire: WireFrontier = field(default_factory=NoWireFrontier)
    file: FileIdentity | MissingFile = field(kw_only=True)

    @property
    def session_file(self) -> str | None:
        return self.source.read_key

    def with_frontier(self, frontier: WireFrontier) -> TranscriptCursor:
        return replace(self, wire=frontier)

    def mark_read(self, reads: MarksSourceRead[SessionCut], viewer: str, runtime: TranscriptRuntime) -> None:
        """There is no painted native transcript to acknowledge."""
