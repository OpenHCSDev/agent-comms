"""Declared lifecycles for the journal's three independent durable records.

These states describe observations and exclusions, never native write or input
capabilities. Only the journal's returned post-fsync ACK can admit an original.
"""

from __future__ import annotations

import hashlib
import json
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState

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
    native_fields: ClassVar[frozenset[str] | None] = None
    permits_failed_exit: ClassVar[bool] = False

    def validate_native(self, evidence: dict, returncode: int) -> None:
        keys = self.native_fields
        if (
            keys is None
            or set(evidence) != keys | {"status"}
            or any(type(evidence[key]) is not str or not evidence[key] for key in keys)
        ):
            raise ValueError("Incomplete native outcome; never replay")
        if returncode and not self.permits_failed_exit:
            raise ValueError("Inconsistent native outcome; never replay")

    def matches_metadata(self, evidence: dict, expected: str) -> bool:
        return True

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[OperationState], ...]: ...


class IntentOperation(OperationState):
    @classmethod
    def successors(cls):
        return (UnknownOperation, CommittedOperation, RefusedOperation, AbortedNoWriteOperation)


class UnknownOperation(OperationState):
    native_fields = frozenset({"reason"})
    permits_failed_exit = True

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
    native_fields = frozenset({"entryId", "revision", "leafId", "metadataDigest"})

    def validate_native(self, evidence, returncode):
        super().validate_native(evidence, returncode)
        digest = evidence["metadataDigest"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid native metadata receipt; never replay")

    def matches_metadata(self, evidence, expected):
        return evidence["metadataDigest"] == expected


class RefusedOperation(TerminalOperation, OperationState):
    pass


class AbortedNoWriteOperation(TerminalOperation, OperationState, declared_name="aborted-no-write"):
    native_fields = frozenset({"revision", "leafId"})


@dataclass(frozen=True)
class SummaryState(DeclaredFamily, LifecycleState, affix="Summary"):
    terminal: ClassVar[bool] = False
    original_eligible: ClassVar[bool] = False
    reservable_commit: ClassVar[bool] = False
    commit_id: ClassVar[None] = None
    decline_reason: ClassVar[None] = None

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[SummaryState], ...]: ...

    @classmethod
    def from_columns(cls, name: str, commit_id: str | None, decline_reason: str | None):
        return cls.decode(name).load(commit_id, decline_reason)

    @classmethod
    @abstractmethod
    def load(cls, commit_id: str | None, decline_reason: str | None) -> SummaryState: ...

    def verifies_original(
        self, journal: CompactionJournal, session: str, operation_id: str, source_json: str
    ) -> bool:
        return False


class UnsettledSummary(SummaryState):
    @classmethod
    def load(cls, commit_id, decline_reason):
        if commit_id is not None or decline_reason is not None:
            raise ValueError("Unsettled summary cannot carry terminal evidence")
        return cls()


class ReservedSummary(UnsettledSummary):
    reservable_commit = True

    @classmethod
    def successors(cls):
        return (UnknownSummary, LinkedSummary, DeclinedPrestartSummary, RefusedSummary)


class UnknownSummary(UnsettledSummary):
    @classmethod
    def successors(cls):
        return (UnknownSummary,)


@dataclass(frozen=True)
class LinkedSummary(SummaryState):
    commit_id: str = field()
    terminal = True
    original_eligible = True

    def __post_init__(self):
        if type(self.commit_id) is not str or not self.commit_id:
            raise ValueError("Linked summary requires its native commit ID")

    @classmethod
    def successors(cls):
        return ()

    @classmethod
    def load(cls, commit_id, decline_reason):
        if decline_reason is not None:
            raise ValueError("Linked summary cannot carry a decline")
        return cls(commit_id)

    def verifies_original(self, journal, session, operation_id, source_json):
        commit = journal.get(self.commit_id)
        intent = json.loads(commit.intent_json)
        return (
            commit.session_file == session
            and commit.state.committed
            and type(intent) is dict
            and intent.get("selectedSummaryOperationId") == operation_id
            and intent.get("selectedSummarySourceDigest")
            == hashlib.sha256(source_json.encode()).hexdigest()
        )


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

    @classmethod
    def load(cls, commit_id, decline_reason):
        if commit_id is not None:
            raise ValueError("Declined summary cannot carry a native commit")
        return cls(decline_reason)

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

    @classmethod
    def successors(cls):
        return ()

    @classmethod
    def load(cls, commit_id, decline_reason):
        if commit_id is not None:
            raise ValueError("Refused summary cannot carry a native commit")
        return cls(decline_reason)


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
class NativeOutcome:
    """One decoded child result; not proof of owner authority or durability."""

    state: OperationState
    evidence: dict

    @classmethod
    def from_wire(cls, evidence: object, returncode: int) -> NativeOutcome:
        if not isinstance(evidence, dict):
            raise ValueError("Invalid native outcome; never replay")
        state = OperationState.decode(evidence.get("status"))()
        state.validate_native(evidence, returncode)
        return cls(state, evidence)

    @classmethod
    def unknown(cls, reason: str) -> NativeOutcome:
        state = UnknownOperation()
        return cls(state, {"status": state.declared_name, "reason": reason})

    def bind_metadata(self, expected: str) -> NativeOutcome:
        if not self.state.matches_metadata(self.evidence, expected):
            return self.unknown("native-metadata-mismatch")
        return self
