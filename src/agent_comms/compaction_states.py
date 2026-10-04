"""Declared lifecycles for the journal's three independent durable records.

These states describe observations and exclusions, never native write or input
capabilities. Only the journal's returned post-fsync ACK can admit an original.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, ClassVar

from .child_process import ChildOutcome
from .compaction_errors import CompactionJournalError
from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState
from .native_revision_text import NativeRevisionText
from .private_path import FileRevision
from .text_digest import TextDigest

if TYPE_CHECKING:
    from .compaction_journal import CompactionJournal
    from .compaction_records import SelectedSummaryAttempt
    from .compaction_records import CompactionOperation
    from .input_disposition import InputDocument
    from .selected_source import SessionRevision



def sql_names(family: type[DeclaredFamily], *, unresolved: bool = False) -> str:
    """DDL/query choices derived from declarations, not a parallel SQL roster."""
    return (
        "("
        + ",".join(
            "'" + member.declared_name.replace("'", "''") + "'"
            for member in family.members_with(family)
            if not unresolved or not member.terminal
        )
        + ")"
    )


@dataclass(frozen=True)
class OperationState(DeclaredFamily, LifecycleState, affix="Operation"):
    terminal: ClassVar[bool] = False
    committed: ClassVar[bool] = False
    def represents_summary(self, operation: CompactionOperation,
                           attempt: SelectedSummaryAttempt) -> bool:
        return False
    def require_committed(self, commit_id: str) -> None:
        raise CompactionJournalError(
            f"Native compaction operation {commit_id} is {self.declared_name}; "
            "reconcile exact ID before any new input"
        )

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[OperationState], ...]: ...


class IntentOperation(OperationState):
    @classmethod
    def successors(cls):
        return (UnknownOperation, CommittedOperation, RefusedOperation, AbortedNoWriteOperation)


class UnknownOperation(OperationState):
    @classmethod
    def successors(cls):
        # Uncertainty can never become a pre-write refusal.
        return (UnknownOperation, CommittedOperation, AbortedNoWriteOperation)


class TerminalOperation:
    terminal = True

    @classmethod
    def successors(cls):
        return ()


class CommittedOperation(TerminalOperation, OperationState):
    committed = True
    def require_committed(self, commit_id: str) -> None:
        return None
    def represents_summary(self, operation: CompactionOperation,
                           attempt: SelectedSummaryAttempt) -> bool:
        operation.require_summary_link(attempt, admit_original=True)
        operation.committed_outcome()
        return True


class RefusedOperation(TerminalOperation, OperationState):
    pass


class AbortedNoWriteOperation(TerminalOperation, OperationState, declared_name="aborted-no-write"):
    pass


@dataclass(frozen=True)
class SummaryState(DeclaredFamily, LifecycleState, affix="Summary"):
    terminal: ClassVar[bool] = False
    original_eligible: ClassVar[bool] = False
    settled_without_original: ClassVar[bool] = False

    def project_outcome(self, attempt: SelectedSummaryAttempt, sequence: int):
        """In-flight requests and native-owned summaries add no journal notice."""
        return None

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[SummaryState], ...]: ...
    def blocks_input(self, attempt: SelectedSummaryAttempt, inputs: InputDocument) -> bool:
        """Only this lifecycle owns whether original-input start can retire its barrier."""
        if self.settled_without_original:
            return False
        if not self.original_eligible:
            return True
        return not attempt.original_has_started(inputs)

    def require_original_admission(self) -> None:
        if not self.original_eligible:
            raise CompactionJournalError("Manual compaction cannot admit an original input")
    def require_commit_reservation(self) -> None:
        raise CompactionJournalError("Selected summary is not a commit reservation")
    def refuse(self, reason: str) -> SummaryState:
        raise CompactionJournalError("Selected summary refusal transition forbidden")
    def fail(self, reason: str) -> SummaryState:
        raise CompactionJournalError("Selected summary failure transition forbidden")
    def retire_unchanged_source(self) -> SummaryState:
        """Settled/link states retain their original disposition during recovery."""
        return self
    def verifies_original(
        self, journal: CompactionJournal, attempt: SelectedSummaryAttempt
    ) -> bool:
        return False


class ReservedSummary(SummaryState):
    def retire_unchanged_source(self) -> SummaryState:
        # A reservation records neither provider completion nor a native write.
        # Recovery must still prove its original source is unchanged under the
        # native writer and that no commit/input binding exists. Provider outcome
        # remains UNKNOWN; this does not restart its request.
        return RetiredUnknownSummary()

    def require_commit_reservation(self) -> None:
        pass
    def refuse(self, reason: str) -> SummaryState:
        return RefusedSummary(reason)
    def fail(self, reason: str) -> SummaryState:
        return FailedSummary(reason)

    @classmethod
    def successors(cls):
        return (
            UnknownSummary,
            LinkedSummary,
            ManualCommittedSummary,
            DeclinedPrestartSummary,
            RefusedSummary,
            FailedSummary,
            RetiredUnknownSummary,
        )


class SummaryOutcome:
    """The original state declaration owns its read-only transcript notice."""

    @property
    @abstractmethod
    def outcome_text(self) -> str: ...

    def project_outcome(self, attempt: SelectedSummaryAttempt, sequence: int):
        from .compaction_outcomes import SelectedCompactionOutcome

        return SelectedCompactionOutcome(attempt, sequence)


class UnknownSummaryOutcome(SummaryOutcome):
    @property
    def outcome_text(self) -> str:
        return "Compaction outcome is UNKNOWN. Inspect the original operation; do not replay."


class RefusedSummaryOutcome(SummaryOutcome):
    decline_reason: str

    @property
    def outcome_text(self) -> str:
        return f"Compaction refused: {self.decline_reason}"


class UnknownSummary(UnknownSummaryOutcome, SummaryState):
    def retire_unchanged_source(self) -> SummaryState:
        return RetiredUnknownSummary()

    @classmethod
    def successors(cls):
        return (UnknownSummary, RetiredUnknownSummary)


class RetiredUnknownSummary(UnknownSummaryOutcome, SummaryState):
    """Provider outcome stays unknown; writer-fenced evidence excludes a native write."""

    terminal = True
    settled_without_original = True

    @classmethod
    def successors(cls):
        return ()


@dataclass(frozen=True)
class FailedSummary(SummaryOutcome, SummaryState):
    """A correlated summary failure attested no native write or original input."""

    reason: str
    terminal = True
    settled_without_original = True

    @property
    def outcome_text(self) -> str:
        return f"Compaction failed: {self.reason}"

    @classmethod
    def successors(cls):
        return ()


@dataclass(frozen=True)
class LinkedSummary(SummaryState):
    commit_id: str = field()
    terminal = True
    original_eligible = True
    def __post_init__(self):
        if not self.commit_id:
            raise ValueError("Linked summary requires its native commit ID")

    @classmethod
    def successors(cls):
        return ()
    def verifies_original(self, journal, attempt):
        commit = journal.operations.get(self.commit_id)
        commit.require_summary_link(attempt, admit_original=True)
        return True


class ManualCommittedSummary(LinkedSummary):
    """Explicit compaction has no original prompt to admit or replay."""

    original_eligible = False
    settled_without_original = True


@dataclass(frozen=True)
class DeclinedPrestartSummary(SummaryState, declared_name="declined-prestart"):
    decline_reason: str = field()
    terminal = True
    original_eligible = True
    def __post_init__(self):
        if self.decline_reason not in {"split_turn", "unsupported"}:
            raise ValueError("Selected summary decline is not a clean skip")

    @classmethod
    def successors(cls):
        return ()
    def verifies_original(self, journal, attempt):
        return True


@dataclass(frozen=True)
class RefusedSummary(RefusedSummaryOutcome, SummaryState):
    """Correlated native prestart refusal, never an original-input admission."""

    decline_reason: str = field()
    terminal = True
    def __post_init__(self):
        if not self.decline_reason or len(self.decline_reason) > 256:
            raise ValueError("Bounded native refusal reason required")
    def retire_unchanged_source(self) -> SummaryState:
        return RetiredRefusalSummary(self.decline_reason)
    def refuse(self, reason: str) -> SummaryState:
        if reason != self.decline_reason:
            return super().refuse(reason)
        return self

    @classmethod
    def successors(cls):
        return (RetiredRefusalSummary,)


@dataclass(frozen=True)
class RetiredRefusalSummary(RefusedSummaryOutcome, SummaryState):
    """Original source custody excluded a native write after a known refusal."""

    decline_reason: str = field()
    terminal = True
    settled_without_original = True

    @classmethod
    def successors(cls):
        return ()
    def __post_init__(self):
        if not self.decline_reason or len(self.decline_reason) > 256:
            raise ValueError("Retired refusal requires its bounded native reason")


@dataclass(frozen=True)
class PublicationState(DeclaredFamily, LifecycleState, affix="Publication"):
    terminal: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[PublicationState], ...]: ...


class PendingPublication(PublicationState):
    @classmethod
    def successors(cls):
        return (ObservedPublication,)


class ObservedPublication(PublicationState):
    terminal = True

    @classmethod
    def successors(cls):
        return ()


@dataclass(frozen=True)
class NativeCommitPosition:
    """The committed native entry/revision/leaf shared by receipt and publication."""

    entry_id: str = field(metadata={"wire_name": "entryId"})
    revision: Annotated[FileRevision, NativeRevisionText]
    leaf_id: str = field(metadata={"wire_name": "leafId"})
    def __post_init__(self):
        if not self.entry_id or not self.leaf_id:
            raise ValueError("Invalid native metadata receipt; never replay")

    def require_entry(self, entry) -> None:
        """A returned commit refers to this original journal entry."""
        if entry.require_entry_id() != self.entry_id:
            raise CompactionJournalError("Original committed source cut differs")


@dataclass(frozen=True)
class CompactionPublishedMetadata(NativeCommitPosition):
    commit_id: str = field(metadata={"wire_name": "commitId"})


class NativeOutcome(DeclaredFamily, affix="NativeOutcome"):
    """Strict native observations, distinct from journal lifecycle authority."""

    family_discriminator = "status"
    state: ClassVar[OperationState]
    permits_failed_exit: ClassVar[bool] = False
    def checked_child(self, outcome: ChildOutcome) -> NativeOutcome:
        if not outcome.successful and not self.permits_failed_exit:
            raise ValueError("Inconsistent native outcome; never replay")
        return self
    def bind_metadata(self, expected: str) -> NativeOutcome:
        return self

    def publication_json(self, commit_id: str) -> str | None:
        return None

    def require_committed(self) -> CommittedNativeOutcome:
        raise CompactionJournalError("Committed operation lacks committed native evidence")

    def journal_json(self) -> str:
        from .field_codec import FieldCodec
        from .retained_task_facts import RetainedTaskFacts

        return RetainedTaskFacts.frame_journal(FieldCodec.encode(self))

    @classmethod
    def read(cls, payload: str) -> NativeOutcome:
        from .field_codec import FieldCodec

        outcome = FieldCodec.decode(cls, json.loads(payload))
        outcome.journal_json()
        return outcome


@dataclass(frozen=True)
class UnknownNativeOutcome(NativeOutcome):
    state = UnknownOperation()
    permits_failed_exit = True
    reason: str
    def __post_init__(self):
        if not self.reason:
            raise ValueError("Native uncertainty requires its observed reason")


@dataclass(frozen=True)
class CommittedNativeOutcome(NativeCommitPosition, NativeOutcome):
    state = CommittedOperation()
    metadata_digest: str = field(metadata={"wire_name": "metadataDigest"})
    def __post_init__(self):
        super().__post_init__()
        TextDigest(self.metadata_digest)
    def bind_metadata(self, expected: str) -> NativeOutcome:
        if self.metadata_digest != expected:
            return UnknownNativeOutcome("native-metadata-mismatch")
        return self
    def require_saved_revision(self, revision: SessionRevision) -> None:
        if self.revision != revision.native:
            raise CompactionJournalError("Selected native result is unavailable: saved revision changed")

    def require_committed(self) -> CommittedNativeOutcome:
        return self

    def publication_json(self, commit_id: str) -> str:
        from .field_codec import FieldCodec

        return json.dumps(FieldCodec.encode(self.publication(commit_id)), sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    def publication(self, commit_id: str) -> CompactionPublishedMetadata:
        return CompactionPublishedMetadata(self.entry_id, self.revision, self.leaf_id, commit_id)


@dataclass(frozen=True)
class AbortedNoWriteNativeOutcome(NativeOutcome, declared_name="aborted-no-write"):
    state = AbortedNoWriteOperation()
    revision: Annotated[FileRevision, NativeRevisionText]
    leaf_id: str = field(metadata={"wire_name": "leafId"})
    def __post_init__(self):
        if not self.leaf_id:
            raise ValueError("Incomplete native no-write receipt")
