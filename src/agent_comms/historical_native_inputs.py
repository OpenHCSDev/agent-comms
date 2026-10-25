"""Historical native input evidence, not an injected-message cursor.

A SQL row recorded only after a live Pi result can be corroborated against the
private session journal and the durable prelaunch native-request binding.
Missing or mismatched equality cannot grant source proof. This historical read
grants no response, recovery, model replay, edit, or current-owner authority.
"""

from __future__ import annotations

import sqlite3
import sys

from agent_comms.native_session_files import NativeSessionFiles
from abc import abstractmethod
from dataclasses import dataclass
from typing import Annotated, TYPE_CHECKING

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination

from .cohort_schema import CohortTable
from agent_comms.native_runtime_input import NativeRuntimeTable
from .declared_family import DeclaredFamily
from .field_codec import InputId, MessageId, WireName, RootId, ThreadLookup
from .native_input_record import (
    FullNativeExecution,
    NativeInputExecution,
    NativeInputRecord,
    NativeInputReference,
    TriageNativeExecution,
)
from .native_pi import NativeContextProof
from .native_prompt_binding import expected_prompt_bindings, expected_prompt_matches_journal
from .native_runtime_input import NativeRuntimeInput
from .selected_triage import SelectedTriage
from .typed_table import TypedRow

if TYPE_CHECKING:
    from .native_entries import NativeEvidenceScope


@dataclass(frozen=True, kw_only=True)
class HistoricalNativeInput(NativeInputRecord, DeclaredFamily, affix="HistoricalNativeInput"):
    wire_root_id: RootId
    source_seq: int
    source_message_id: MessageId
    assignment_id: Annotated[str, WireName("claim_id")]
    execution: NativeInputExecution
    input_id: InputId
    owner_lookup: ThreadLookup
    owner_generation: int
    context: NativeContextProof
    native_reference: NativeInputReference
    # Prelaunch binding facts: None means no binding was durably written
    # before launch (crash ordering), so equality cannot be established.
    expected_prompt_digest: str | None = None
    expected_prompt_equality_established: bool = False

    @abstractmethod
    def proves_triage_source(self, evidence) -> bool: ...

    @property
    def reference(self) -> NativeInputReference:
        return self.native_reference


@dataclass(frozen=True, kw_only=True)
class TriageHistoricalNativeInput(HistoricalNativeInput):
    execution: TriageNativeExecution
    decision: type[SelectedTriage]

    def proves_triage_source(self, evidence) -> bool:
        return self.expected_prompt_equality_established and self.decision.proves_source(evidence)


@dataclass(frozen=True, kw_only=True)
class FailedTriageHistoricalNativeInput(HistoricalNativeInput):
    """Canonical failed claim plus live-recorded context; no decision fabricated."""

    execution: TriageNativeExecution

    def proves_triage_source(self, evidence) -> bool:
        return self.expected_prompt_equality_established


@dataclass(frozen=True, kw_only=True)
class FullHistoricalNativeInput(HistoricalNativeInput):
    execution: FullNativeExecution

    def proves_triage_source(self, evidence) -> bool:
        return False


@dataclass(frozen=True)
class _HistoricalSource(TypedRow):
    input_id: InputId
    assignment_id: str
    wire_seq: int
    message_id: MessageId


def _selected_sources(columns: str, condition: str) -> str:
    """One recipient's sealed selected sources joined to the native inputs that took them."""
    return (
        f"SELECT {columns} FROM native_runtime_input n "
        f"JOIN ({NativeRuntimeInput.source_membership_sql()}) member ON member.input_id=n.input_id "
        "JOIN wake_claims c ON c.assignment_id=member.assignment_id "
        "JOIN claim_batch_members m ON m.claim_id=c.assignment_id "
        "AND m.recipient_lookup=c.recipient_lookup "
        "JOIN claim_batch_receipts r ON r.wire_root_id=m.wire_root_id "
        "AND r.wire_seq=m.wire_seq AND r.message_id=c.message_id AND r.sealed=1 "
        "JOIN cohort_delivery_receipts d ON d.wire_root_id=r.wire_root_id "
        "AND d.wire_seq=r.wire_seq AND d.claim_id=c.assignment_id "
        "AND d.recipient_lookup=n.owner_lookup AND d.kind='selected' "
        f"WHERE r.wire_root_id=? AND c.recipient_lookup=? AND {condition} "
        "AND n.owner_lookup=c.recipient_lookup AND n.session_entry_id IS NOT NULL"
    )


def first_foreign_claim_source(
    db: sqlite3.Connection, *, wire_root_id: RootId, recipient_lookup: ThreadLookup,
    after_seq: int, claimed_generation: int,
) -> int:
    """The first source after ``after_seq`` taken by an input another claim sent.

    A cursor owner proves only inputs its own claim sent (CursorOwner.matches_prefix),
    so no coverage scan publishes past this source. With none, the scan may reach the bus end.
    """
    (seq,) = db.execute(
        _selected_sources("COALESCE(MIN(c.wire_seq), ?)",
                          "c.wire_seq>? AND n.sent_owner_claimed_generation IS NOT ?"),
        (sys.maxsize, wire_root_id, recipient_lookup, after_seq, claimed_generation),
    ).fetchone()
    return int(seq)


def read_historical_native_inputs(
    store: Coordination,
    *,
    wire_root_id: RootId,
    recipient_lookup: ThreadLookup,
    source_seq: int,
    source_reads: NativeEvidenceScope,
) -> tuple[HistoricalNativeInput, ...]:
    """Return at most triage and FULL proof for one sealed selected source.

    Caller-supplied identity must come from a trusted bus/owner snapshot, never
    from model arguments. This read does not look up a live owner or even assert
    that the original bus remains valid now. It cannot authorize work. In
    particular, do NOT derive max-seq or a cursor from these rows alone: gaps
    and PASSIVE/no-wake deliveries have different semantics. A separate
    current-owner cursor additionally verifies the canonical bus prefix and
    requires a just-settled input in the owner's current input generation.
    """
    with source_reads.borrow():
        if source_seq <= 0:
            raise ValueError("historical input requires a positive source sequence")
        if store.session._connection.in_transaction:
            raise IdentityConflict("historical native input view requires a committed snapshot")
        with store.session.read():
            db = store.session._connection
            CohortTable.require_installed(db)
            NativeRuntimeTable.require_installed(db)
            sources = _HistoricalSource.read(
                db.execute(
                    _selected_sources("n.input_id,c.assignment_id,c.wire_seq,c.message_id", "c.wire_seq=?"),
                    (wire_root_id, recipient_lookup, source_seq),
                )
            )
            rows = []
            for source in sources:
                row = NativeRuntimeInput.one(db, input_id=source.input_id)
                if not row:
                    raise IdentityConflict(
                        "historical native input disappeared inside its snapshot"
                    )
                # A retried FULL attempt saw the input, but its execution moved
                # on; only the execution's current attempt can prove the source.
                if not row.execution.retried_in(db):
                    rows.append((source, row))
        if len(rows) > 2 or len({type(row.execution) for _source, row in rows}) != len(rows):
            raise IdentityConflict("historical source has ambiguous native input evidence")
        source_reads.observe_inputs(tuple(row.require_context_proof() for _source, row in rows))
        with expected_prompt_bindings(store, (row.input_id for _source, row in rows)) as bindings:
            rows.sort(key=lambda item: type(item[1].execution).declared_position())
        evidence: list[HistoricalNativeInput] = []
        for source, row in rows:
            original_session = row.require_session_identity()
            session_file = original_session.path
            recorded = row.require_context_proof()
            try:
                # Journal alone cannot promote an unrecorded or uncertain input.
                # The immutable SQL row is already present from the live event;
                # this check only corroborates its message-bearing context facts.
                corroborated = recorded.corroborates_input(
                    row.input_id, evidence=source_reads.for_source(session_file),
                    files=NativeSessionFiles.for_identity(original_session, root=store.session.path.parent,
                                             owner=row.owner_identity),
                )
            except (OSError, ValueError) as error:
                raise IdentityConflict("historical native context evidence is unavailable") from error
            if not corroborated:
                raise IdentityConflict("historical native context differs from live-recorded proof")
            original_session.require_context(recorded)
            binding = bindings.get(row.input_id)
            if binding is not None:
                # A binding must name exactly this reserved input; anything else is
                # corruption, not a failed equality join.
                if binding.identity != row.identity:
                    raise IdentityConflict("prelaunch binding does not match this live proof")
                if binding.wire_root_id != wire_root_id:
                    raise IdentityConflict("Prelaunch binding names another canonical wire root")
            equality = binding is not None and expected_prompt_matches_journal(
                session_file, binding, evidence=source_reads.for_source(session_file)
            )
            evidence.append(
                row.execution.historical_proof(
                    row,
                    lifecycle=store.assignments.get(source.assignment_id).lifecycle,
                    wire_root_id=wire_root_id,
                    source_seq=source.wire_seq,
                    source_message_id=source.message_id,
                    assignment_id=source.assignment_id,
                    input_id=row.input_id,
                    owner_lookup=row.owner_lookup,
                    owner_generation=row.owner_generation,
                    context=recorded,
                    native_reference=row.reference,
                    expected_prompt_digest=binding.expected_prompt_digest if binding is not None else None,
                    expected_prompt_equality_established=equality,
                )
            )
        return tuple(evidence)
