"""Historical native input evidence, not an injected-message cursor.

A SQL row recorded only after a live Pi result can be corroborated against the
private session journal and the durable prelaunch native-request binding.
Missing or mismatched equality cannot grant source proof. This historical read
grants no response, recovery, model replay, edit, or current-owner authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_cohort import _assert_schema as assert_cohort_schema
from .coordination_store import IdentityConflict, MutationStore
from .native_pi import NativeContextProof, NativePiUnavailable, _read_native_context_evidence
from .native_prompt_binding import (
    expected_prompt_matches_journal,
    read_expected_prompt_binding,
)


@dataclass(frozen=True, slots=True)
class HistoricalNativeInput:
    wire_root_id: str
    source_seq: int
    source_message_id: str
    claim_id: str
    stage: str
    input_id: str
    owner_lookup: str
    owner_thread: str
    owner_generation: int
    execution_id: str | None
    attempt_ordinal: int | None
    triage_result: str | None  # 'ignore' or 'full'; FULL stage has no triage verdict.
    context: NativeContextProof
    # Prelaunch binding facts: None means no binding was durably written
    # before launch (crash ordering), so equality cannot be established.
    expected_prompt_digest: str | None = None
    expected_prompt_equality_established: bool = False


def read_historical_native_inputs(
    store: MutationStore,
    *,
    wire_root_id: str,
    recipient_lookup: str,
    source_seq: int,
) -> tuple[HistoricalNativeInput, ...]:
    """Return at most triage and FULL proof for one sealed selected source.

    Caller-supplied identity must come from a trusted bus/owner snapshot, never
    from model arguments. This read does not look up a live owner or even assert
    that the original bus remains valid now. It cannot authorize work. In
    particular, do NOT derive max-seq or a cursor from these rows alone: gaps
    and PASSIVE/no-wake deliveries have different semantics. A separate
    current-owner cursor additionally verifies the canonical bus prefix and
    requires a just-settled input in the live admission epoch.
    """
    if (
        type(store) is not MutationStore
        or type(wire_root_id) is not str
        or len(wire_root_id) != 32
        or any(character not in "0123456789abcdef" for character in wire_root_id)
        or type(recipient_lookup) is not str
        or len(recipient_lookup) != 32
        or any(character not in "0123456789abcdef" for character in recipient_lookup)
        or type(source_seq) is not int
        or source_seq <= 0
    ):
        raise ValueError("historical input requires exact trusted root, lookup, and sequence")
    if store._connection.in_transaction:
        raise IdentityConflict("historical native input view requires a committed snapshot")
    with store._read_transaction():
        db = store._connection
        assert_cohort_schema(db)
        assert_native_runtime_schema(db)
        rows = db.execute(
            "SELECT n.input_id,n.stage,n.claim_id,n.owner_lookup,n.owner_thread,"
            "n.owner_generation,n.execution_id,n.attempt_ordinal,n.verdict,n.session_id,"
            "n.session_file,n.session_entry_id,n.request_generation,n.llm_context_digest,"
            "c.wire_seq,c.message_id "
            "FROM native_runtime_inputs n "
            "JOIN wake_claims c ON c.claim_id=n.claim_id "
            "JOIN claim_batch_members m ON m.claim_id=c.claim_id "
            "AND m.recipient_lookup=c.recipient_lookup "
            "JOIN claim_batch_receipts r ON r.wire_root_id=m.wire_root_id "
            "AND r.wire_seq=m.wire_seq AND r.message_id=c.message_id AND r.sealed=1 "
            "JOIN cohort_delivery_receipts d ON d.wire_root_id=r.wire_root_id "
            "AND d.wire_seq=r.wire_seq AND d.claim_id=c.claim_id "
            "AND d.recipient_lookup=n.owner_lookup AND d.kind='selected' "
            "WHERE r.wire_root_id=? AND c.recipient_lookup=? AND c.wire_seq=? "
            "AND n.owner_lookup=c.recipient_lookup AND n.session_id IS NOT NULL "
            "ORDER BY CASE n.stage WHEN 'triage' THEN 0 ELSE 1 END LIMIT 3",
            (wire_root_id, recipient_lookup, source_seq),
        ).fetchall()
    if len(rows) > 2 or len({row["stage"] for row in rows}) != len(rows):
        raise IdentityConflict("historical source has ambiguous native input evidence")
    expected_dir = (store.path.parent / "native-sessions" / recipient_lookup).absolute()
    evidence: list[HistoricalNativeInput] = []
    for row in rows:
        session_file = Path(row["session_file"])
        if not session_file.is_absolute() or session_file.parent != expected_dir:
            raise IdentityConflict("historical native session belongs to another recipient")
        recorded = NativeContextProof(
            row["input_id"],
            row["session_id"],
            row["session_entry_id"],
            row["request_generation"],
            row["llm_context_digest"],
            session_file,
        )
        try:
            # Journal alone cannot promote an unrecorded or uncertain input.
            # The immutable SQL row is already present from the live event;
            # this check only corroborates its message-bearing context facts.
            observed = _read_native_context_evidence(session_file, row["input_id"])
        except (OSError, ValueError, NativePiUnavailable) as error:
            raise IdentityConflict("historical native context evidence is unavailable") from error
        if observed != recorded:
            raise IdentityConflict("historical native context differs from live-recorded proof")
        binding = read_expected_prompt_binding(store, row["input_id"])
        if binding is not None:
            # A binding must name exactly this reserved input; anything else is
            # corruption, not a failed equality join.
            if (
                binding.wire_root_id != wire_root_id
                or binding.stage != row["stage"]
                or binding.claim_id != row["claim_id"]
                or binding.execution_id != row["execution_id"]
                or binding.attempt_ordinal != row["attempt_ordinal"]
                or binding.owner_lookup != row["owner_lookup"]
                or binding.owner_thread != row["owner_thread"]
                or binding.owner_generation != row["owner_generation"]
                or binding.source_seq != row["wire_seq"]
                or binding.message_id != row["message_id"]
            ):
                raise IdentityConflict("prelaunch binding does not match this live proof")
            equality = expected_prompt_matches_journal(session_file, binding)
        else:
            equality = False
        evidence.append(
            HistoricalNativeInput(
                wire_root_id,
                row["wire_seq"],
                row["message_id"],
                row["claim_id"],
                row["stage"],
                row["input_id"],
                row["owner_lookup"],
                row["owner_thread"],
                row["owner_generation"],
                row["execution_id"],
                row["attempt_ordinal"],
                row["verdict"],
                recorded,
                binding.expected_prompt_digest if binding is not None else None,
                equality,
            )
        )
    return tuple(evidence)
