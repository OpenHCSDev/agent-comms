"""Declared lifecycles for the journal's three independent durable records.

These states describe observations and exclusions, never native write or input
capabilities. Only the journal's returned post-fsync ACK can admit an original.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from .child_process import ChildOutcome
from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState
from .text_digest import TextDigest

if TYPE_CHECKING:
    from .compaction_journal import CompactionJournal


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

    def require_committed(self, commit_id: str) -> None:
        from .compaction_journal import CompactionJournalError

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


class RefusedOperation(TerminalOperation, OperationState):
    pass


class AbortedNoWriteOperation(TerminalOperation, OperationState, declared_name="aborted-no-write"):
    pass


@dataclass(frozen=True)
class SummaryState(DeclaredFamily, LifecycleState, affix="Summary"):
    terminal: ClassVar[bool] = False
    original_eligible: ClassVar[bool] = False
    settled_without_original: ClassVar[bool] = False
    reconcile_unchanged_source: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[SummaryState], ...]: ...

    def require_original_admission(self) -> None:
        from .compaction_journal import CompactionJournalError

        if not self.original_eligible:
            raise CompactionJournalError("Manual compaction cannot admit an original input")

    def require_commit_reservation(self) -> None:
        from .compaction_journal import CompactionJournalError

        raise CompactionJournalError("Selected summary is not a commit reservation")

    def manual_recovery(self) -> SummaryState:
        from .compaction_journal import CompactionJournalError

        raise CompactionJournalError(
            "Prior selected compaction is uncertain; inspect compaction-status, never replay"
        )

    def refuse(self, reason: str) -> SummaryState:
        from .compaction_journal import CompactionJournalError

        raise CompactionJournalError("Selected summary refusal transition forbidden")

    def fail(self, reason: str) -> SummaryState:
        from .compaction_journal import CompactionJournalError

        raise CompactionJournalError("Selected summary failure transition forbidden")

    def retire_unchanged_source(self) -> SummaryState:
        from .compaction_journal import CompactionJournalError

        raise CompactionJournalError("Selected summary is not an interrupted no-write candidate")

    def verifies_original(
        self, journal: CompactionJournal, session: str, operation_id: str, source_json: str
    ) -> bool:
        return False


class ReservedSummary(SummaryState):
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
        )


class UnknownSummary(SummaryState):
    reconcile_unchanged_source = True

    def retire_unchanged_source(self) -> SummaryState:
        return RetiredUnknownSummary()

    @classmethod
    def successors(cls):
        return (UnknownSummary, RetiredUnknownSummary)


class RetiredUnknownSummary(SummaryState):
    """Provider outcome stays unknown; writer-fenced evidence excludes a native write."""

    terminal = True
    settled_without_original = True

    @classmethod
    def successors(cls):
        return ()


@dataclass(frozen=True)
class FailedSummary(SummaryState):
    """A correlated summary failure attested no native write or original input."""

    reason: str
    terminal = True
    settled_without_original = True

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

    def verifies_original(self, journal, session, operation_id, source_json):
        commit = journal.get(self.commit_id)
        from .compaction_journal import SelectedSummaryAttempt

        commit.require_summary_link(
            SelectedSummaryAttempt(operation_id, session, source_json, self), admit_original=True,
        )
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

    def verifies_original(self, journal, session, operation_id, source_json):
        return True


@dataclass(frozen=True)
class RefusedSummary(SummaryState):
    """Correlated native prestart refusal, never an original-input admission."""

    decline_reason: str = field()
    terminal = True

    def __post_init__(self):
        if not self.decline_reason or len(self.decline_reason) > 256:
            raise ValueError("Bounded native refusal reason required")

    def manual_recovery(self) -> SummaryState:
        return RetiredRefusalSummary(self.decline_reason)

    def refuse(self, reason: str) -> SummaryState:
        if reason != self.decline_reason:
            return super().refuse(reason)
        return self

    @classmethod
    def successors(cls):
        return (RetiredRefusalSummary,)


@dataclass(frozen=True)
class RetiredRefusalSummary(SummaryState):
    """An explicit manual command acknowledged a known no-provider refusal."""

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
    revision: str
    leaf_id: str = field(metadata={"wire_name": "leafId"})

    def __post_init__(self):
        if not self.entry_id or not self.revision or not self.leaf_id:
            raise ValueError("Invalid native metadata receipt; never replay")


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

    def publication_json(self, commit_id: str) -> str:
        from .field_codec import FieldCodec

        return json.dumps(FieldCodec.encode(self.publication(commit_id)), sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    def publication(self, commit_id: str) -> CompactionPublishedMetadata:
        return CompactionPublishedMetadata(self.entry_id, self.revision, self.leaf_id, commit_id)


@dataclass(frozen=True)
class AbortedNoWriteNativeOutcome(NativeOutcome, declared_name="aborted-no-write"):
    state = AbortedNoWriteOperation()
    revision: str
    leaf_id: str = field(metadata={"wire_name": "leafId"})

    def __post_init__(self):
        if not self.revision or not self.leaf_id:
            raise ValueError("Incomplete native no-write receipt")
