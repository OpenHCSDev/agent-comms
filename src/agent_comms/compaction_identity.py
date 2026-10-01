"""Exact journal record identities; none grants execution or replay authority."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .child_process import ProcessIdentity
from .compaction_errors import CompactionJournalError
from .field_codec import FieldCodec
from .text_digest import TextDigest
from .thread_identity import ThreadIncarnation

if TYPE_CHECKING:
    from .compaction_records import EnrolledPrivateSession, SelectedSummaryAttempt



@dataclass(frozen=True)
class SummaryOperationIdentity:
    session_file: str
    operation_id: str


@dataclass(frozen=True)
class SelectedCommitReference:
    """The selected-summary portion of the journal's native commit intent.

    Other intent fields belong to the native commit owner. Only this declared
    projection is decoded here; no reader guesses linkage from a partial shape.
    """

    operation_id: str = field(metadata={"wire_name": "selectedSummaryOperationId"})
    source_digest: str | None = field(default=None, metadata={
        "wire_name": "selectedSummarySourceDigest", "wire_omit_default": True,
    })

    @classmethod
    def from_intent(cls, intent: dict[str, Any]) -> SelectedCommitReference:
        from .retained_task_facts import RetainedTaskFacts

        intent = FieldCodec.decode(dict[str, Any], intent)
        reference = FieldCodec.decode(cls, {
            wire: intent[wire] for _, wire in FieldCodec._fields(cls) if wire in intent
        })
        RetainedTaskFacts.frame_journal(FieldCodec.encode(reference))
        return reference

    def identity(self, session_file: str) -> SummaryOperationIdentity:
        return SummaryOperationIdentity(session_file, self.operation_id)

    def require_source(self, source_json: str) -> None:
        if self.source_digest != TextDigest.of(source_json).value:
            raise CompactionJournalError("Selected native intent source digest required")


@dataclass(frozen=True)
class FreshCoverageIdentity:
    incarnation: ThreadIncarnation
    creator: ProcessIdentity
    owner_lookup: str


@dataclass(frozen=True)
class ReturnedFreshEnrollment:
    path: Path
    enrollment: EnrolledPrivateSession


@dataclass(frozen=True)
class ReturnedSummaryTerminal:
    path: Path
    attempt: SelectedSummaryAttempt


@dataclass(frozen=True)
class JournalCustody:
    """A particular journal inode in its issuing process, never just a PID."""

    path: Path
    device: int
    inode: int
    process: ProcessIdentity

    @classmethod
    def capture(cls, path: Path) -> JournalCustody:
        info = path.stat()
        return cls(path, info.st_dev, info.st_ino, ProcessIdentity.capture(os.getpid()))
