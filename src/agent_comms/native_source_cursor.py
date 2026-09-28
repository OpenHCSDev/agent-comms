"""Current-owner private source coverage cursor, never a work/ACK/write permit.

A cursor is published only from a live native proof already committed in SQL,
corroborated against the prelaunch binding and native journal. No historical
input is promoted when an owner restarts: a new admission epoch starts at zero.
No consumer may skip a selected claim or retry an uncertain input from this
projection. The authoritative sealed claims still drive execution.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from dataclasses import dataclass

from .bus_publication import stable_thread_lookup
from .coordinated_runtime_schema import assert_native_runtime_schema
from .coordination_response import _response_boundary
from .coordination_store import IdentityConflict, MutationStore, StaleFence
from .historical_native_inputs import HistoricalNativeInput, read_historical_native_inputs
from .message_bus import MessageBus
from .private_bus_checkpoint import PrefixWitness, verify_private_bus_checkpoint_unlocked
from .proven_source_coverage import ProvenSourceCoverage, read_proven_source_coverage
from .threads import Thread

# The old-root canonical bus read still has an 8 MiB / 1,000-row ceiling.
# A checkpointed root pages complete addressed sources; neither its SQL
# index nor its source sequence is native proof or an injected ACK.
_MAX_COVERAGE_PAGES = 32  # 3,200 addressed initials per bounded owner pass.
_MAX_SOURCE_BYTES = 8 * 1024 * 1024


def _source_witness_unlocked(bus: MessageBus) -> PrefixWitness | tuple[int, int, int, str]:
    """Recheck exact certified revision, or hash a bounded legacy bus."""
    marker = bus.log._private_marker_unlocked()
    if marker.get("checkpoint_version") == 1:
        return verify_private_bus_checkpoint_unlocked(bus.log, marker)
    descriptor = os.open(bus.log.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SOURCE_BYTES:
            raise IdentityConflict("current cursor source exceeds bounded canonical bus")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            contents = source.read(_MAX_SOURCE_BYTES + 1)
        if len(contents) != info.st_size:
            raise IdentityConflict("current cursor source changed while fingerprinting")
        return (info.st_dev, info.st_ino, info.st_size, hashlib.sha256(contents).hexdigest())
    finally:
        os.close(descriptor)


def _source_witness(bus: MessageBus) -> PrefixWitness | tuple[int, int, int, str]:
    certified = bus.log.path.with_name("private_bus_checkpoint.sqlite3").exists()
    with bus.log.locked(blocking=False, max_bus_bytes=None if certified else _MAX_SOURCE_BYTES):
        return _source_witness_unlocked(bus)


def _bounded_coverage_pages(
    bus: MessageBus,
    store: MutationStore,
    root_id: str,
    lookup: str,
    *,
    through_seq: int | None = None,
) -> ProvenSourceCoverage:
    """Reverify each bounded canonical page; stop at a selected-proof gap.

    The page cursor is in-memory only. A durable SQL cursor is written only
    after all preceding pages are checked, and no caller-provided high-water
    can make us skip an initial. ``through_seq`` is used only when verifying
    an existing persisted cursor: even its alleged prefix is rescanned.
    """
    covered = 0
    injected: list[int] = []
    no_wake: list[int] = []
    source_witness: PrefixWitness | None = None
    for _ in range(_MAX_COVERAGE_PAGES):
        page = read_proven_source_coverage(
            bus,
            store,
            wire_root_id=root_id,
            recipient_lookup=lookup,
            after_seq=covered,
            partial=True,
        )
        if page.covered_seq < covered:
            raise IdentityConflict("canonical source coverage regressed between pages")
        if page.source_witness is not None:
            if source_witness is not None and page.source_witness != source_witness:
                raise IdentityConflict("certified source changed between coverage pages")
            source_witness = page.source_witness
        elif source_witness is not None:
            raise IdentityConflict("certified source disappeared between coverage pages")
        covered = page.covered_seq
        injected.extend(page.injected_source_seqs)
        no_wake.extend(page.no_wake_seqs)
        if through_seq is not None and covered >= through_seq:
            return ProvenSourceCoverage(
                root_id,
                lookup,
                covered,
                tuple(injected),
                tuple(no_wake),
                page.blocked_seq,
                page.more_initials,
                source_witness,
            )
        if page.blocked_seq is not None or not page.more_initials:
            return ProvenSourceCoverage(
                root_id,
                lookup,
                covered,
                tuple(injected),
                tuple(no_wake),
                page.blocked_seq,
                page.more_initials,
                source_witness,
            )
    raise IdentityConflict("source coverage exceeded bounded canonical page budget")


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


def _prefix_evidence(
    store: MutationStore,
    root_id: str,
    lookup: str,
    coverage: ProvenSourceCoverage,
    *,
    through_seq: int | None = None,
) -> tuple[HistoricalNativeInput, ...]:
    """Inspect every selected source of the canonical covered prefix, not its maximum."""
    return tuple(
        evidence
        for source_seq in coverage.injected_source_seqs
        if through_seq is None or source_seq <= through_seq
        for evidence in read_historical_native_inputs(
            store, wire_root_id=root_id, recipient_lookup=lookup, source_seq=source_seq
        )
    )


def _same_epoch_prefix(
    db: sqlite3.Connection,
    evidence: tuple[HistoricalNativeInput, ...],
    lookup: str,
    owner_name: str,
    generation: int,
    epoch: int,
) -> bool:
    """Reject an old native proof even in a legacy persisted cursor prefix."""
    for item in evidence:
        row = db.execute(
            "SELECT owner_lookup,owner_thread,owner_generation,sent_owner_admission_epoch,"
            "stage,claim_id,session_id,request_generation "
            "FROM native_runtime_inputs WHERE input_id=?",
            (item.input_id,),
        ).fetchone()
        if row is None or tuple(row) != (
            lookup,
            owner_name,
            generation,
            epoch,
            item.stage,
            item.claim_id,
            item.context.session_id,
            item.context.request_generation,
        ):
            return False
    return True


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
    source_witness = _source_witness(bus)
    coverage = _bounded_coverage_pages(bus, store, wire_root_id, lookup)
    injected_seq = coverage.injected_source_seqs[-1] if coverage.injected_source_seqs else 0
    proof = _last_source_proof(store, wire_root_id, lookup, injected_seq)
    # Historical journal reads need a committed SQL snapshot. Collect the
    # exact IDs before the write transaction, then verify their immutable SQL
    # receipt identities again inside its live-owner fence.
    prefix_evidence = _prefix_evidence(store, wire_root_id, lookup, coverage)
    with _response_boundary(bus, blocking=False) as registry, store._transaction() as db:
        marker = bus.log._private_marker_unlocked()
        if _source_witness_unlocked(bus) != source_witness:
            raise IdentityConflict("current cursor canonical source changed before commit")
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
        if prior is None and coverage.covered_seq == 0:
            return None  # A blocked first source is not a zero-valued cursor.
        if prior is not None and (
            prior.owner_thread != owner.name
            or prior.owner_generation != owner_generation
            or coverage.covered_seq < prior.covered_seq
            or injected_seq < prior.injected_seq
        ):
            raise IdentityConflict("current cursor would change owner or regress")
        # Checking only the newest proof could borrow a prior owner's
        # historical selected input as a bridge across a new admission epoch.
        # Every selected source in this covered prefix must belong to this
        # live owner generation and admission epoch, including triage+FULL.
        if not _same_epoch_prefix(
            db,
            prefix_evidence,
            lookup,
            owner.name,
            owner_generation,
            owner_admission_epoch,
        ):
            return prior  # Do not borrow historical native acceptance.
        if proof is None:
            # An absent-audience or sealed no-wake prefix may progress even if
            # the just-committed input lies after an UNKNOWN selected gap.
            # It carries no injected source or native-input pointer.
            pass
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
                "sent_owner_admission_epoch,session_id,request_generation "
                "FROM native_runtime_inputs WHERE input_id=?",
                (proof.input_id,),
            ).fetchone()
            if (
                reserved is not None
                and reserved["sent_owner_admission_epoch"] != owner_admission_epoch
            ):
                if prior is None or injected_seq > prior.injected_seq:
                    return prior  # An old native input cannot seed a new admission.
                raise IdentityConflict("current cursor input admission differs")
            if reserved is None or tuple(reserved) != (
                proof.stage,
                proof.claim_id,
                lookup,
                owner.name,
                owner_generation,
                owner_admission_epoch,
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
        marker = bus.log._private_marker_unlocked()
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
            if row is not None and row["input_id"] is not None:
                input_row = store._connection.execute(
                    "SELECT sent_owner_admission_epoch,owner_lookup,owner_thread,"
                    "owner_generation,claim_id,stage,session_id,request_generation "
                    "FROM native_runtime_inputs WHERE input_id=?",
                    (row["input_id"],),
                ).fetchone()
                if input_row is None or input_row["sent_owner_admission_epoch"] != epoch:
                    raise IdentityConflict("current cursor input admission differs")
                if tuple(input_row) != (
                    epoch,
                    lookup,
                    owner_name,
                    person.generation,
                    row["claim_id"],
                    row["stage"],
                    row["session_id"],
                    row["request_generation"],
                ):
                    raise IdentityConflict("current cursor proof differs from journal")
        cursor = _cursor_from_row(row) if row is not None else None
        generation = person.generation
    if cursor is not None:
        source_witness = _source_witness(bus)
        if cursor.owner_thread != owner_name or cursor.owner_generation != generation:
            raise IdentityConflict("current native cursor owner identity differs")
        # A persisted pointer is rechecked against the current canonical bus,
        # never inferred from a self-consistent SQL/journal claim alone. Do
        # this outside the registry lock: coverage takes the bus lock itself.
        coverage = _bounded_coverage_pages(
            bus, store, wire_root_id, lookup, through_seq=cursor.covered_seq
        )
        if cursor.covered_seq > coverage.covered_seq or (
            cursor.injected_seq > 0 and cursor.injected_seq not in coverage.injected_source_seqs
        ):
            raise IdentityConflict("current native cursor exceeds canonical source proof")
        # A cursor from an older implementation may point at a current-epoch
        # *last* input while silently spanning a selected gen1/old-epoch input.
        # Reopen must reject the whole persisted prefix, not bless that row.
        prefix_evidence = _prefix_evidence(
            store, wire_root_id, lookup, coverage, through_seq=cursor.covered_seq
        )
        with store._read_transaction():
            assert_native_runtime_schema(store._connection)
            if not _same_epoch_prefix(
                store._connection, prefix_evidence, lookup, owner_name, generation, epoch
            ):
                raise IdentityConflict("current cursor borrows historical owner source proof")
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
        if cursor is not None and _source_witness_unlocked(bus) != source_witness:
            raise IdentityConflict("current cursor canonical source changed while reading")
        current = registry.threads.get(owner_name)
        status = registry.statuses.get(owner_name)
        if (
            bus.log._private_marker_unlocked()["wire_root_id"] != wire_root_id
            or current is None
            or status is None
            or not status.active
            or (
                current.name,
                current.created_at,
                current.pid,
                current.role,
                current.worktree,
                current.active_turn,
                current.goal,
            )
            != (
                actual.name,
                actual.created_at,
                actual.pid,
                actual.role,
                actual.worktree,
                actual.active_turn,
                actual.goal,
            )
            or current.pid != os.getpid()
            or registry.admission_generations.get(owner_name) != epoch
        ):
            raise StaleFence("current native cursor owner changed while reading")
        # The canonical pages and earlier SQL proof view ran outside this
        # boundary. A supported participant-generation advance can occur
        # between them without changing the registry. Recheck SQL while all
        # wire/bus/registry locks are held; never return a stale gen1 cursor
        # after a same-owner gen2 advance or a replacement SQL cursor row.
        with store._read_transaction():
            assert_native_runtime_schema(store._connection)
            fresh = store._participant(lookup)
            if (
                not fresh.committed
                or fresh.owner_thread != owner_name
                or fresh.generation != generation
            ):
                raise StaleFence("current native cursor participant generation changed")
            fresh_row = store._connection.execute(
                "SELECT * FROM native_runtime_source_cursors WHERE wire_root_id=? "
                "AND recipient_lookup=? AND owner_generation=? AND owner_admission_epoch=?",
                (wire_root_id, lookup, generation, epoch),
            ).fetchone()
            if (fresh_row is None) != (cursor is None) or (
                fresh_row is not None and _cursor_from_row(fresh_row) != cursor
            ):
                raise StaleFence("current native cursor SQL row changed while reading")
    return cursor
