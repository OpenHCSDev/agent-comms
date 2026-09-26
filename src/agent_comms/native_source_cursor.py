"""Current-owner private source coverage cursor, never a work/ACK/write permit.

A cursor is published only from a live native proof already committed in SQL,
corroborated against the prelaunch binding and native journal. No historical
input is promoted when an owner restarts: a new admission epoch starts at zero.
No consumer may skip a selected claim or retry an uncertain input from this
projection. The authoritative sealed claims still drive execution.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from .bus_publication import stable_thread_lookup
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_response import _response_boundary
from .coordination_store import IdentityConflict, MutationStore, StaleFence
from .declarations import MessageBus, Thread
from .historical_native_inputs import HistoricalNativeInput, read_historical_native_inputs
from .proven_source_coverage import read_proven_source_coverage


@dataclass(frozen=True, slots=True)
class CurrentNativeCursor:
    wire_root_id: str
    recipient_lookup: str
    owner_thread: str
    owner_generation: int
    owner_admission_epoch: int
    covered_seq: int
    injected_seq: int
    input_id: str | None
    claim_id: str | None
    stage: str | None
    session_id: str | None
    request_generation: int | None


def _last_source_proof(
    store: MutationStore, root_id: str, lookup: str, source_seq: int
) -> HistoricalNativeInput | None:
    if source_seq == 0:
        return None
    evidence = read_historical_native_inputs(
        store, wire_root_id=root_id, recipient_lookup=lookup, source_seq=source_seq
    )
    matching = [row for row in evidence if row.expected_prompt_equality_established]
    if not matching:
        raise IdentityConflict("current cursor source lacks live-bound native proof")
    return matching[-1]  # triage IGNORE or the required final FULL stage


def _cursor_from_row(row: sqlite3.Row) -> CurrentNativeCursor:
    return CurrentNativeCursor(
        row["wire_root_id"],
        row["recipient_lookup"],
        row["owner_thread"],
        row["owner_generation"],
        row["owner_admission_epoch"],
        row["covered_seq"],
        row["injected_seq"],
        row["input_id"],
        row["claim_id"],
        row["stage"],
        row["session_id"],
        row["request_generation"],
    )


def advance_current_native_cursor(
    bus: MessageBus,
    store: MutationStore,
    *,
    wire_root_id: str,
    owner: Thread,
    owner_admission_epoch: int,
    owner_generation: int,
    committed_input_id: str | None,
) -> CurrentNativeCursor | None:
    """Record a safe prefix for this *current* owner, never replay past input.

    The caller supplies only the input freshly completed in this turn, or None
    for no-wake/absent-audience progress. Historical proofs from another owner
    epoch cannot initialize or advance a new current cursor. The bus snapshot
    is bounded and read-only; the final owner and SQL witnesses are rechecked
    under canonical wire→bus→registry→SQL locks before one monotonic row write.
    """
    if (
        type(bus) is not MessageBus
        or type(store) is not MutationStore
        or type(owner) is not Thread
        or type(owner_admission_epoch) is not int
        or owner_admission_epoch <= 0
        or type(owner_generation) is not int
        or owner_generation <= 0
        or (committed_input_id is not None and type(committed_input_id) is not str)
    ):
        raise ValueError("current cursor requires exact coordinator and owner identities")
    lookup = stable_thread_lookup(owner.created_at)
    coverage = read_proven_source_coverage(
        bus, store, wire_root_id=wire_root_id, recipient_lookup=lookup
    )
    injected_seq = coverage.injected_source_seqs[-1] if coverage.injected_source_seqs else 0
    proof = _last_source_proof(store, wire_root_id, lookup, injected_seq)
    with _response_boundary(bus, blocking=False) as registry, store._transaction() as db:
        marker = bus._private_marker_unlocked()
        actual = registry.threads.get(owner.name)
        status = registry.statuses.get(owner.name)
        if (
            marker["wire_root_id"] != wire_root_id
            or actual is None
            or status is None
            or not status.active
            or registry.admission_generations.get(owner.name) != owner_admission_epoch
            or actual.pid != os.getpid()
            or actual.goal != owner.goal
            or (
                actual.name,
                actual.created_at,
                actual.pid,
                actual.role,
                actual.worktree,
                actual.active_turn,
            )
            != (
                owner.name,
                owner.created_at,
                owner.pid,
                owner.role,
                owner.worktree,
                owner.active_turn,
            )
        ):
            raise StaleFence("current cursor owner or private root changed")
        assert_native_runtime_schema(db)
        person = store._participant(lookup)
        if (
            not person.committed
            or person.owner_thread != owner.name
            or person.generation != owner_generation
        ):
            raise StaleFence("current cursor recipient generation changed")
        old = db.execute(
            "SELECT * FROM native_runtime_source_cursors WHERE wire_root_id=? "
            "AND recipient_lookup=? AND owner_generation=? AND owner_admission_epoch=?",
            (wire_root_id, lookup, owner_generation, owner_admission_epoch),
        ).fetchone()
        prior = _cursor_from_row(old) if old is not None else None
        if prior is not None and (
            prior.owner_thread != owner.name
            or prior.owner_generation != owner_generation
            or coverage.covered_seq < prior.covered_seq
            or injected_seq < prior.injected_seq
        ):
            raise IdentityConflict("current cursor would change owner or regress")
        if proof is None:
            if committed_input_id is not None:
                return prior  # Earlier source is UNKNOWN: do not skip it.
        else:
            if (
                proof.owner_lookup != lookup
                or proof.owner_thread != owner.name
                or proof.owner_generation != owner_generation
                or proof.source_seq != injected_seq
            ):
                raise IdentityConflict("current cursor native proof belongs to another owner")
            # A fresh result may advance the injected sequence. Otherwise the
            # existing exact input must already have been accepted in THIS
            # admission epoch. No old journal proof can initialize a new one.
            if prior is None or injected_seq > prior.injected_seq:
                if proof.input_id != committed_input_id:
                    return prior  # Historical proof is not this live turn.
            elif prior.input_id != proof.input_id:
                raise IdentityConflict("current cursor native input changed")
            elif committed_input_id is not None and proof.input_id != committed_input_id:
                return prior  # Current source lies after an unproven gap.
            reserved = db.execute(
                "SELECT stage,claim_id,owner_lookup,owner_thread,owner_generation,"
                "session_id,request_generation FROM native_runtime_inputs WHERE input_id=?",
                (proof.input_id,),
            ).fetchone()
            if reserved is None or tuple(reserved) != (
                proof.stage,
                proof.claim_id,
                lookup,
                owner.name,
                owner_generation,
                proof.context.session_id,
                proof.context.request_generation,
            ):
                raise IdentityConflict("current cursor differs from committed native receipt")
        values = (
            wire_root_id,
            lookup,
            owner.name,
            owner_generation,
            owner_admission_epoch,
            coverage.covered_seq,
            injected_seq,
            proof.input_id if proof else None,
            proof.claim_id if proof else None,
            proof.stage if proof else None,
            proof.context.session_id if proof else None,
            proof.context.request_generation if proof else None,
        )
        if prior is None:
            db.execute(
                "INSERT INTO native_runtime_source_cursors VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        elif coverage.covered_seq > prior.covered_seq or injected_seq > prior.injected_seq:
            updated = db.execute(
                "UPDATE native_runtime_source_cursors SET covered_seq=?,injected_seq=?,"
                "input_id=?,claim_id=?,stage=?,session_id=?,request_generation=? "
                "WHERE wire_root_id=? AND recipient_lookup=? AND owner_generation=? "
                "AND owner_admission_epoch=? "
                "AND covered_seq=? AND injected_seq=?",
                (
                    coverage.covered_seq,
                    injected_seq,
                    proof.input_id if proof else None,
                    proof.claim_id if proof else None,
                    proof.stage if proof else None,
                    proof.context.session_id if proof else None,
                    proof.context.request_generation if proof else None,
                    wire_root_id,
                    lookup,
                    owner_generation,
                    owner_admission_epoch,
                    prior.covered_seq,
                    prior.injected_seq,
                ),
            )
            if updated.rowcount != 1:
                raise StaleFence("current cursor monotonic update lost its fence")
    return CurrentNativeCursor(*values)


def read_current_native_cursor(
    bus: MessageBus,
    store: MutationStore,
    *,
    wire_root_id: str,
    owner_name: str,
) -> CurrentNativeCursor | None:
    """Read only THIS active owner epoch; never reconstruct a new cursor from history.

    Reconnect after owner replacement sees None even when old SQL/journal rows
    remain available for inspection. The returned cursor is informational and
    does not authorize skipping a sealed claim, ACK, write, response or retry.
    """
    if type(bus) is not MessageBus or type(store) is not MutationStore:
        raise ValueError("current native cursor needs actual private stores")
    with _response_boundary(bus, blocking=False) as registry:
        marker = bus._private_marker_unlocked()
        actual = registry.threads.get(owner_name)
        status = registry.statuses.get(owner_name)
        epoch = registry.admission_generations.get(owner_name)
        if (
            marker["wire_root_id"] != wire_root_id
            or actual is None
            or status is None
            or not status.active
            or actual.pid != os.getpid()
            or epoch is None
        ):
            raise StaleFence("current native cursor has no matching live owner")
        lookup = stable_thread_lookup(actual.created_at)
        with store._read_transaction():
            assert_native_runtime_schema(store._connection)
            person = store._participant(lookup)
            if not person.committed or person.owner_thread != owner_name:
                raise StaleFence("current native cursor recipient is not committed")
            row = store._connection.execute(
                "SELECT * FROM native_runtime_source_cursors WHERE wire_root_id=? "
                "AND recipient_lookup=? AND owner_generation=? AND owner_admission_epoch=?",
                (wire_root_id, lookup, person.generation, epoch),
            ).fetchone()
        cursor = _cursor_from_row(row) if row is not None else None
        generation = person.generation
    if cursor is not None:
        if cursor.owner_thread != owner_name or cursor.owner_generation != generation:
            raise IdentityConflict("current native cursor owner identity differs")
        # A persisted pointer is rechecked against the current canonical bus,
        # never inferred from a self-consistent SQL/journal claim alone. Do
        # this outside the registry lock: coverage takes the bus lock itself.
        coverage = read_proven_source_coverage(
            bus, store, wire_root_id=wire_root_id, recipient_lookup=lookup
        )
        if cursor.covered_seq > coverage.covered_seq or (
            cursor.injected_seq > 0 and cursor.injected_seq not in coverage.injected_source_seqs
        ):
            raise IdentityConflict("current native cursor exceeds canonical source proof")
        proof = _last_source_proof(store, wire_root_id, lookup, cursor.injected_seq)
        if (cursor.injected_seq == 0 and cursor.input_id is not None) or (
            proof is not None
            and (
                cursor.input_id != proof.input_id
                or cursor.claim_id != proof.claim_id
                or cursor.stage != proof.stage
                or cursor.session_id != proof.context.session_id
                or cursor.request_generation != proof.context.request_generation
                or proof.owner_generation != generation
                or proof.owner_thread != owner_name
            )
        ):
            raise IdentityConflict("current native cursor proof differs from journal")
    with _response_boundary(bus, blocking=False) as registry:
        current = registry.threads.get(owner_name)
        status = registry.statuses.get(owner_name)
        if (
            bus._private_marker_unlocked()["wire_root_id"] != wire_root_id
            or current is None
            or status is None
            or not status.active
            or current.created_at != actual.created_at
            or current.pid != os.getpid()
            or registry.admission_generations.get(owner_name) != epoch
        ):
            raise StaleFence("current native cursor owner changed while reading")
    return cursor
