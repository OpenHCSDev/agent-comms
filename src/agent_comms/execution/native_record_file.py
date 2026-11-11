from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from agent_comms.coordination.errors import TranscriptChanged
from agent_comms.execution.native_records import NativeRecord
from agent_comms.execution.native_runtime_input import NativeRuntimeInput, PublishedReplyRevision
from agent_comms.execution.native_session_reopen import PiSessionId
from agent_comms.messaging.transcript_read_mark import MarksTranscriptRead
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.storage.private_path import FileIdentity, FileRevision, MissingFile



def reverse_records(
    path: Path, before: int, *, floor: int = 0, stream=None
) -> Generator[tuple[int, int, bytes]]:
    """Scan offsets in fixed chunks, then read each complete record exactly once.

    Record size is independent of scan-buffer size. In particular, images/tool
    output exceeding a buffer are neither dropped nor repeatedly concatenated.
    """
    with (path.open("rb") if stream is None else nullcontext(stream)) as stream:
        position = end = before
        while position > floor:
            count = min(position - floor, 65536)
            position -= count
            stream.seek(position)
            block = stream.read(count)
            stop = len(block)
            while (boundary := block.rfind(b"\n", 0, stop)) >= 0:
                start = position + boundary + 1
                stop = boundary
                if start == end:
                    continue
                stream.seek(start)
                yield start, end, stream.read(end - start)
                end = start
        if end > floor:
            stream.seek(max(0, floor - 1))
            if floor == 0 or stream.read(1) == b"\n":
                stream.seek(floor)
                yield floor, end, stream.read(end - floor)


def record_end(stream: BinaryIO, size: int) -> int:
    """The end of the last newline-terminated record within the first ``size`` bytes."""
    position = size
    while position > 0:
        count = min(position, 65536)
        stream.seek(position - count)
        boundary = stream.read(count).rfind(b"\n")
        if boundary >= 0:
            return position - count + boundary + 1
        position -= count
    return 0


class RecordsCut(DeclaredFamily, affix="Cut"):
    """How far a thread's own records reached when its page was read.

    Members supply ``offset``: the page position just after their last record.
    """

    offset: int

    def holds(self, current: RecordsCut, through: int) -> bool:
        """Whether ``current`` still contains the records this page read through."""
        return type(current) is type(self) and self.contains(current, through)

    @abstractmethod
    def contains(self, current, through: int) -> bool: ...

    @abstractmethod
    def draft_current(self, current: RecordsCut) -> bool:
        """Whether this cut can still accompany the source's current draft."""

    def file_identity(self) -> FileIdentity | MissingFile:
        """The file whose bytes this cut's offsets index; records kept outside a file have none."""
        return MissingFile()

    def indexes(self, file: FileIdentity | MissingFile, offset: int) -> bool:
        """Whether an offset read from ``file`` still indexes this source's records. Records kept
        outside a file are never replaced underneath their offsets."""
        return True


class SessionCut(RecordsCut):
    """The saved session's actual file state, including one not written yet."""

    def draft_current(self, current: RecordsCut) -> bool:
        # Native drafts belong to the ACP stream, not this saved-record reader.
        return self.holds(current, self.offset)

    @abstractmethod
    def mark_read(self, reads: MarksTranscriptRead, viewer: str, source: str, through: int) -> None: ...

    @abstractmethod
    def publication_revision(
        self, root: Path, reader: ReadsNativeInputs, recipient: RecordedThreadIncarnation
    ) -> PublishedReplyRevision: ...


@dataclass(frozen=True)
class MissingSessionCut(SessionCut):
    offset = 0

    def contains(self, current: SessionCut, through: int) -> bool:
        return through == 0

    def mark_read(self, reads: MarksTranscriptRead, viewer: str, source: str, through: int) -> None:
        # No file means no written transcript, just as unread counts return zero.
        pass

    def publication_revision(
        self, root: Path, reader: ReadsNativeInputs, recipient: RecordedThreadIncarnation
    ) -> PublishedReplyRevision:
        return PublishedReplyRevision(0, 0)


@dataclass(frozen=True)
class PresentSessionCut(SessionCut):
    revision: FileRevision
    # Just after the last newline-terminated record; a line still being
    # appended is not a record yet, so no cursor can point inside it.
    offset: int

    def contains(self, current: PresentSessionCut, through: int) -> bool:
        # A session is append-only within one inode; replacement changes the cut.
        return self.revision.holds_cut(current.revision, through)

    def file_identity(self) -> FileIdentity:
        return self.revision.identity

    def indexes(self, file: FileIdentity | MissingFile, offset: int) -> bool:
        # Appends keep the inode; a rewrite through os.replace moves every offset past 0.
        return offset == 0 or file == self.revision.identity

    def mark_read(self, reads: MarksTranscriptRead, viewer: str, source: str, through: int) -> None:
        if not 0 <= through <= self.offset:
            raise TranscriptChanged()
        reads.mark_transcript(viewer, source, self.revision.identity.inode,
                              through, self.revision.size)

    def publication_revision(
        self, root: Path, reader: ReadsNativeInputs, recipient: RecordedThreadIncarnation
    ) -> PublishedReplyRevision:
        return NativeRuntimeInput.publication_revision(root, reader, recipient.lookup)


class ReadsNativeInputs(ABC):
    """The original native file supplies its session identity and input ancestry."""

    path: Path

    @property
    @abstractmethod
    def session_id(self) -> PiSessionId | None: ...

    @abstractmethod
    def inputs_for(self, records: Sequence[NativeRecord], *, floor: int = 0) -> tuple[NativeRecord, ...]: ...
