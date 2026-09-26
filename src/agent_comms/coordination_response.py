"""Default-off, owner-fenced response publication across the coordinator and bus.

Tx1 freezes an intent without a bus append. For the bounded append/Tx2, acquire
the shared wire lock, bus lock, registry lock, then SQLite BEGIN IMMEDIATE.
The registry lock is retained through the fsynced append and SQL terminal
commit; an explicitly witnessed live owner can be checked on that revision.
A stop before the boundary refuses publication, whereas a concurrent stop
linearizes after commit. The
rollback-journal transaction only spans one bounded, fsynced local bus row;
ordinary bus reads are completed *before* acquiring a coordinator transaction.
A crash may still leave a frozen PUBLISHING intent plus a durable bus row. The
read-only resolver can settle an EXISTING exact receipt using the retained
current owner fence, but never creates an absent row or manufactures owner loss.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .bus_publication import stable_thread_lookup
from .coordination import (
    AttemptPhase,
    ClaimDisposition,
    ExecutionStatus,
    ObligationState,
    OwnerFence,
    PublicationIntent,
    RecoverySnapshot,
    canonical_publication_key,
)
from .coordination_cohort import _assert_schema as _assert_cohort_schema
from .coordination_store import (
    AlreadyApplied,
    Applied,
    IdentityConflict,
    MutationStore,
    PublicationActivationBlocked,
    PublicationUncertain,
    RecoveryBlocked,
    StaleFence,
    _digest,
)
from .declarations import (
    ActiveTurn,
    Message,
    MessageBus,
    MessageType,
    RegistrySnapshot,
    ThreadRole,
    _store_lock,
)
from .wake import WakeDecision, derive_exact_reply_target

_RESPONSE_DDL = (
    (
        "response_schema_meta",
        """CREATE TABLE response_schema_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            version INTEGER NOT NULL CHECK (version = 1),
            ddl_digest TEXT NOT NULL CHECK (length(ddl_digest) = 64)
        ) STRICT""",
    ),
    (
        "publication_append_dispatches",
        """CREATE TABLE publication_append_dispatches (
            execution_id TEXT PRIMARY KEY REFERENCES publication_intents(execution_id),
            wire_root_id TEXT NOT NULL CHECK (
                length(wire_root_id) = 32 AND wire_root_id NOT GLOB '*[^0-9a-f]*'),
            owner_generation INTEGER NOT NULL CHECK (owner_generation > 0),
            attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal > 0),
            dispatched_at_ms INTEGER NOT NULL CHECK (dispatched_at_ms >= 0)
        ) STRICT, WITHOUT ROWID""",
    ),
    (
        "publication_dispatch_update_guard",
        """CREATE TRIGGER publication_dispatch_update_guard
        BEFORE UPDATE ON publication_append_dispatches
        BEGIN SELECT RAISE(ABORT, 'publication dispatch is frozen'); END""",
    ),
    (
        "publication_dispatch_delete_guard",
        """CREATE TRIGGER publication_dispatch_delete_guard
        BEFORE DELETE ON publication_append_dispatches
        BEGIN SELECT RAISE(ABORT, 'publication dispatch cannot be deleted'); END""",
    ),
    (
        "response_schema_meta_update_guard",
        """CREATE TRIGGER response_schema_meta_update_guard
        BEFORE UPDATE ON response_schema_meta
        BEGIN SELECT RAISE(ABORT, 'response schema metadata is frozen'); END""",
    ),
    (
        "response_schema_meta_delete_guard",
        """CREATE TRIGGER response_schema_meta_delete_guard
        BEFORE DELETE ON response_schema_meta
        BEGIN SELECT RAISE(ABORT, 'response schema metadata cannot be deleted'); END""",
    ),
)
_RESPONSE_DDL_DIGEST = hashlib.sha256(
    json.dumps(_RESPONSE_DDL, separators=(",", ":")).encode()
).hexdigest()


def _assert_response_schema(db: sqlite3.Connection) -> None:
    try:
        meta = db.execute(
            "SELECT version,ddl_digest FROM response_schema_meta WHERE singleton=1"
        ).fetchone()
    except sqlite3.OperationalError as error:
        raise PublicationActivationBlocked("private response schema is not installed") from error
    if meta is None or tuple(meta) != (1, _RESPONSE_DDL_DIGEST):
        raise PublicationActivationBlocked("private response schema version is unsupported")
    actual = {
        row["name"]: row["sql"]
        for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type IN ('table','trigger') "
            "AND (name LIKE 'response_schema_%' OR name LIKE 'publication_dispatch_%' "
            "OR name='publication_append_dispatches')"
        )
    }
    if actual != dict(_RESPONSE_DDL) or db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise PublicationActivationBlocked("private response schema has drifted")


def install_private_response_schema(store: MutationStore) -> None:
    """Explicit fresh-root private migration; ordinary store construction is inert."""
    if type(store) is not MutationStore:
        raise TypeError("response schema requires the actual coordinator store")
    with store._transaction() as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='response_schema_meta'"
        ).fetchone()
        if exists is None:
            for _, statement in _RESPONSE_DDL:
                db.execute(statement)
            db.execute("INSERT INTO response_schema_meta VALUES (1,1,?)", (_RESPONSE_DDL_DIGEST,))
        _assert_response_schema(db)


@contextmanager
def _response_boundary(bus: MessageBus, *, blocking: bool = True) -> Iterator[RegistrySnapshot]:
    """Total lock order: shared wire -> bus -> registry -> SQLite.

    Raw keyed appends acquire bus then registry; Comms register takes shared
    wire then registry. No registry API that reacquires its lock may be used in
    this boundary: the bus append receives this immutable loaded revision.
    """
    with (
        _store_lock(bus._path.parent / "wire", blocking=blocking),
        _store_lock(bus._path, blocking=blocking),
        _store_lock(bus._registry._path, blocking=blocking),
    ):
        yield bus._registry._snapshot_unlocked()


@dataclass(frozen=True, slots=True)
class LiveResponseOwner:
    """A specific running turn, not a PID-only assertion or a status bit."""

    name: str
    recipient_lookup: str
    pid: int
    created_at: float
    worktree: str
    active_turn: ActiveTurn
    admission_generation: int


def _require_live_registry_owner(
    snapshot: RegistrySnapshot,
    fence: OwnerFence,
    owner_pid: int | None,
    owner_witness: LiveResponseOwner | None,
) -> None:
    if owner_witness is not None:
        if (
            type(owner_witness) is not LiveResponseOwner
            or owner_witness.name != fence.owner_thread
            or type(owner_witness.active_turn) is not ActiveTurn
            or owner_witness.active_turn.owner_pid != owner_witness.pid
            or owner_witness.active_turn.admission_generation != owner_witness.admission_generation
            or type(owner_witness.admission_generation) is not int
            or owner_witness.admission_generation < 1
            or type(owner_witness.pid) is not int
            or (owner_pid is not None and owner_pid != owner_witness.pid)
        ):
            raise StaleFence("response turn witness is invalid")
        owner_pid = owner_witness.pid
    if owner_pid is None:
        return  # Legacy explicit response fixtures, never the coordinated runner.
    if type(owner_pid) is not int or owner_pid != os.getpid():
        raise StaleFence("response witness does not own the current process")
    thread = snapshot.threads.get(fence.owner_thread)
    status = snapshot.statuses.get(fence.owner_thread)
    if (
        thread is None
        or status is None
        or not status.active
        or thread.role is not ThreadRole.AGENT
        or thread.name != fence.owner_thread
        or thread.pid != owner_pid
        or (
            owner_witness is not None
            and (
                thread.created_at != owner_witness.created_at
                or stable_thread_lookup(thread.created_at) != owner_witness.recipient_lookup
                or thread.worktree != owner_witness.worktree
                or thread.active_turn != owner_witness.active_turn
                or snapshot.admission_generations.get(thread.name)
                != owner_witness.admission_generation
            )
        )
    ):
        raise StaleFence("response owner turn stopped or changed before publication")


def _require_bound_stores(bus: MessageBus, store: MutationStore) -> None:
    if type(bus) is not MessageBus or type(store) is not MutationStore:
        raise TypeError("response requires real, explicitly gated bus and coordinator stores")
    if bus._private_response_writes is not True:
        raise PublicationActivationBlocked("private response publication is disabled")
    if (
        store.path.name != "coordination.sqlite3"
        or Path(bus._path.parent).absolute() != Path(store.path.parent).absolute()
        or bus._registry._path.absolute() != (bus._path.parent / "registry.json").absolute()
    ):
        raise IdentityConflict("response bus and coordinator have different trusted roots")


def _require_cohort_claims(
    store: MutationStore,
    bus: MessageBus,
    snapshot: RecoverySnapshot,
    wire_root_id: str,
    *,
    terminal: bool = False,
) -> None:
    """Bind selected SQL facts to the ORIGINAL bus origin and exact reply route.

    A DM reply targets the original sender, not the DM's incoming target.
    Caller already holds the bus file lock before the SQL transaction.
    """
    db = store._connection
    _assert_response_schema(db)
    _assert_cohort_schema(db)
    if not snapshot.claims or snapshot.obligation is None:
        raise IdentityConflict("wire response requires selected claims and obligation")
    metadata = bus._private_marker_unlocked()
    if metadata["wire_root_id"] != wire_root_id:
        raise IdentityConflict("cohort bus root changed")
    originals = {
        message.seq: initial
        for message, _receipt, initial in bus._verified_private_rows_unlocked(metadata)
        if initial is not None
    }
    for claim in snapshot.claims:
        initial = originals.get(claim.wire_seq)
        receipt = db.execute(
            "SELECT r.message_id,r.exact_target,r.envelope_digest,r.audience_digest,"
            "r.decisions_digest,r.resolver_version,r.policy_version,m.recipient_lookup,"
            "d.canonical_thread FROM claim_batch_members m JOIN claim_batch_receipts r "
            "ON r.wire_root_id=m.wire_root_id AND r.wire_seq=m.wire_seq "
            "JOIN cohort_delivery_receipts d "
            "ON d.wire_root_id=m.wire_root_id AND d.wire_seq=m.wire_seq "
            "AND d.claim_id=m.claim_id AND d.kind='selected' "
            "WHERE m.claim_id=? AND m.wire_root_id=? AND r.sealed=1",
            (claim.claim_id, wire_root_id),
        ).fetchone()
        if initial is None or receipt is None:
            raise IdentityConflict("response claim lacks original bus/cohort authority")
        selected = {
            recipient.recipient_lookup
            for recipient, decision in zip(
                initial.audience.recipients, initial.decisions, strict=True
            )
            if type(decision) is WakeDecision
        }
        if (
            initial.wire_root_id != wire_root_id
            or claim.message_id != initial.message.message_id
            or tuple(receipt)
            != (
                initial.message.message_id,
                initial.message.target,
                initial.audience.wire_envelope_digest,
                initial.audience.digest,
                initial.decisions_digest,
                claim.resolver_version,
                claim.policy_version,
                claim.recipient_lookup,
                claim.recipient,
            )
            or claim.recipient_lookup not in selected
            or derive_exact_reply_target(initial.message) != snapshot.execution.exact_target
            or claim.exact_target != snapshot.execution.exact_target
            or claim.disposition
            is not (ClaimDisposition.COMPLETED if terminal else ClaimDisposition.ENGAGED)
        ):
            raise IdentityConflict("response claim conflicts with original selected bus route")


def _require_final_owner(
    store: MutationStore,
    bus: MessageBus,
    fence: OwnerFence,
    wire_root_id: str,
    owner_witness: LiveResponseOwner | None = None,
) -> RecoverySnapshot:
    snapshot, attempt = store._assert_fence(fence)
    execution = snapshot.execution
    if owner_witness is not None and execution.owner_lookup != owner_witness.recipient_lookup:
        raise StaleFence("response turn belongs to a different SQL recipient")
    if (
        execution.status is not ExecutionStatus.ACTIVE
        or attempt.phase is not AttemptPhase.SETTLING
        or not (attempt.backend_done and attempt.process_dead)
        or snapshot.obligation is None
        or snapshot.obligation.exact_target != execution.exact_target
        or execution.exact_target is None
    ):
        raise RecoveryBlocked("response requires final model/death evidence and exact wire route")
    _require_cohort_claims(store, bus, snapshot, wire_root_id)
    return snapshot


def _intent_matches_request(
    intent: PublicationIntent,
    *,
    execution_id: str,
    sender: str,
    target: str,
    payload: str,
    kind: MessageType,
    notice: bool,
    timestamp: float | None,
) -> bool:
    return (
        intent.execution_id == execution_id
        and intent.sender == sender
        and intent.exact_target == target
        and intent.payload == payload
        and intent.message_type is kind
        and intent.notice is notice
        and (timestamp is None or intent.timestamp == timestamp)
    )


def prepare_fenced_response(
    store: MutationStore,
    bus: MessageBus,
    fence: OwnerFence,
    payload: str,
    *,
    message_type: MessageType = MessageType.INFO,
    notice: bool = False,
    timestamp: float | None = None,
    owner_pid: int | None = None,
    owner_witness: LiveResponseOwner | None = None,
) -> Applied[PublicationIntent] | AlreadyApplied[PublicationIntent]:
    """Tx1: freeze the only legal reply envelope and publishing obligation.

    This is an explicitly test-gated coordinator API, not Pi context proof or an
    authorization to infer backend completion from text. Only the current owner
    fence plus final backend/death evidence can prepare it.
    """
    _require_bound_stores(bus, store)
    if (
        type(fence) is not OwnerFence
        or type(message_type) is not MessageType
        or type(notice) is not bool
    ):
        raise TypeError("response requires exact fence, message type and notice")
    if not isinstance(payload, str) or not payload:
        raise ValueError("response payload must be nonempty")
    with _response_boundary(bus) as registry_snapshot:
        _require_live_registry_owner(registry_snapshot, fence, owner_pid, owner_witness)
        metadata = bus._private_marker_unlocked()
        # A corrupt row anywhere is never accepted as an absent publication.
        tuple(bus._verified_private_rows_unlocked(metadata))
        with store._transaction() as db:
            snapshot = _require_final_owner(
                store, bus, fence, str(metadata["wire_root_id"]), owner_witness
            )
            execution = snapshot.execution
            assert execution.exact_target is not None
            existing = snapshot.publication_intent
            if existing is not None:
                if (
                    snapshot.obligation is None
                    or snapshot.obligation.state is not ObligationState.PUBLISHING
                    or not _intent_matches_request(
                        existing,
                        execution_id=execution.execution_id,
                        sender=execution.owner_thread,
                        target=execution.exact_target,
                        payload=payload,
                        kind=message_type,
                        notice=notice,
                        timestamp=timestamp,
                    )
                ):
                    raise IdentityConflict("prepared response envelope conflicts")
                return AlreadyApplied(existing)
            if (
                snapshot.obligation is None
                or snapshot.obligation.state is not ObligationState.PENDING
            ):
                raise IdentityConflict("response obligation cannot prepare a new intent")
            when = time.time() if timestamp is None else timestamp
            candidate = Message(
                execution.owner_thread,
                execution.exact_target,
                payload,
                message_type,
                timestamp=when,
                notice=notice,
            )
            intent = PublicationIntent(
                execution_id=execution.execution_id,
                sender=execution.owner_thread,
                exact_target=execution.exact_target,
                message_type=message_type,
                notice=notice,
                timestamp=when,
                payload=payload,
                payload_digest=hashlib.sha256(payload.encode()).hexdigest(),
                publication_key=canonical_publication_key(
                    execution.execution_id, execution.exact_target
                ),
                expected_message_id=candidate.message_id,
            )
            db.execute(
                "INSERT INTO publication_intents "
                "(execution_id,sender,exact_target,message_type,notice,timestamp,payload,"
                "payload_digest,publication_key,expected_message_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    intent.execution_id,
                    intent.sender,
                    intent.exact_target,
                    intent.message_type.value,
                    int(intent.notice),
                    intent.timestamp,
                    intent.payload,
                    intent.payload_digest,
                    intent.publication_key,
                    intent.expected_message_id,
                ),
            )
            db.execute(
                "UPDATE obligations SET state='publishing',revision=revision+1,updated_at_ms=? "
                "WHERE execution_id=? AND state='pending' AND revision=?",
                (
                    store._now(snapshot.obligation.updated_at_ms),
                    execution.execution_id,
                    snapshot.obligation.revision,
                ),
            )
            return Applied(intent)


def _terminal_replay(
    store: MutationStore,
    fence: OwnerFence,
    bus: MessageBus,
    wire_root_id: str,
    owner_witness: LiveResponseOwner | None = None,
) -> AlreadyApplied[RecoverySnapshot] | None:
    """A finished immutable receipt may be re-read, never re-published."""
    snapshot = store.snapshot(fence.execution_id)
    if snapshot.execution.status is not ExecutionStatus.COMPLETED:
        return None
    if (
        owner_witness is not None
        and snapshot.execution.owner_lookup != owner_witness.recipient_lookup
    ):
        raise StaleFence("finished response belongs to a different SQL recipient")
    attempt = snapshot.attempt
    if (
        attempt is None
        or attempt.owner_thread != fence.owner_thread
        or attempt.owner_generation != fence.owner_generation
        or attempt.attempt_ordinal != fence.attempt_ordinal
        or attempt.owner_token_digest != _digest(fence.token)
        or snapshot.publication_intent is None
        or snapshot.publication_receipt is None
    ):
        raise StaleFence("finished response is not this owner's original attempt")
    _require_cohort_claims(store, bus, snapshot, wire_root_id, terminal=True)
    matched, _, _ = bus._keyed_receipt_unlocked(snapshot.publication_intent)
    if matched is None or (
        matched.message_id != snapshot.publication_receipt.message_id
        or matched.seq != snapshot.publication_receipt.seq
    ):
        raise PublicationUncertain("published SQL receipt has no exact durable bus row")
    return AlreadyApplied(snapshot)


def _settle_fenced_response(
    store: MutationStore,
    bus: MessageBus,
    fence: OwnerFence,
    *,
    allow_append: bool,
    owner_pid: int | None,
    owner_witness: LiveResponseOwner | None,
) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
    _require_bound_stores(bus, store)
    if type(fence) is not OwnerFence:
        raise TypeError("response settlement requires an exact owner fence")
    # One registry revision is held through dispatch, append and Tx2. SQL's
    # current owner generation remains guarded by the transaction fence.
    with _response_boundary(bus) as registry_snapshot:
        _require_live_registry_owner(registry_snapshot, fence, owner_pid, owner_witness)
        metadata = bus._private_marker_unlocked()
        wire_root_id = str(metadata["wire_root_id"])
        first_dispatch = False
        if allow_append:
            # A durable dispatch barrier BEFORE the external append distinguishes
            # a first attempt from a lost response/crash. If it commits but the
            # process dies before appending, no automatic retry is authorized.
            with store._transaction() as db:
                _assert_response_schema(db)
                terminal = _terminal_replay(store, fence, bus, wire_root_id, owner_witness)
                if terminal is not None:
                    return terminal
                snapshot = _require_final_owner(store, bus, fence, wire_root_id, owner_witness)
                if (
                    snapshot.publication_intent is None
                    or snapshot.obligation is None
                    or snapshot.obligation.state is not ObligationState.PUBLISHING
                    or snapshot.publication_intent.sender != snapshot.execution.owner_thread
                    or snapshot.publication_intent.exact_target != snapshot.execution.exact_target
                ):
                    raise IdentityConflict("no frozen publishing intent for current owner")
                prior = db.execute(
                    "SELECT wire_root_id,owner_generation,attempt_ordinal "
                    "FROM publication_append_dispatches WHERE execution_id=?",
                    (fence.execution_id,),
                ).fetchone()
                if prior is None:
                    db.execute(
                        "INSERT INTO publication_append_dispatches VALUES (?,?,?,?,?)",
                        (
                            fence.execution_id,
                            wire_root_id,
                            fence.owner_generation,
                            fence.attempt_ordinal,
                            store._now(snapshot.execution.updated_at_ms),
                        ),
                    )
                    first_dispatch = True
                elif tuple(prior) != (
                    wire_root_id,
                    fence.owner_generation,
                    fence.attempt_ordinal,
                ):
                    raise IdentityConflict("response dispatch belongs to another root or owner")
        with store._transaction() as db:
            _assert_response_schema(db)
            terminal = _terminal_replay(store, fence, bus, wire_root_id, owner_witness)
            if terminal is not None:
                return terminal
            snapshot = _require_final_owner(store, bus, fence, wire_root_id, owner_witness)
            attempt = snapshot.attempt
            if attempt is None:
                raise RecoveryBlocked("response attempt disappeared")
            intent = snapshot.publication_intent
            if (
                intent is None
                or snapshot.obligation is None
                or snapshot.obligation.state is not ObligationState.PUBLISHING
                or intent.sender != snapshot.execution.owner_thread
                or intent.exact_target != snapshot.execution.exact_target
            ):
                raise IdentityConflict("no frozen publishing intent for current owner")
            dispatch = db.execute(
                "SELECT wire_root_id,owner_generation,attempt_ordinal "
                "FROM publication_append_dispatches WHERE execution_id=?",
                (fence.execution_id,),
            ).fetchone()
            if dispatch is None or tuple(dispatch) != (
                wire_root_id,
                fence.owner_generation,
                fence.attempt_ordinal,
            ):
                raise PublicationUncertain("no matching durable append dispatch")
            matched, _, _ = bus._keyed_receipt_unlocked(intent)
            if matched is None:
                if not first_dispatch:
                    raise PublicationUncertain("no keyed bus receipt; no resend authorized")
                # This call created the durable dispatch barrier, and owns the
                # only authorized first append while its final fence is locked.
                matched = bus._publish_keyed_response_unlocked(
                    intent, registry_snapshot=registry_snapshot
                )
            if matched.message_id != intent.expected_message_id:
                raise PublicationUncertain("bus receipt conflicts with immutable intent")
            now = store._now(max(snapshot.execution.updated_at_ms, attempt.updated_at_ms))
            db.execute(
                "INSERT INTO publication_receipts(execution_id,seq,message_id,received_at_ms) "
                "VALUES (?,?,?,?)",
                (intent.execution_id, matched.seq, matched.message_id, now),
            )
            obligation = snapshot.obligation
            db.execute(
                "UPDATE obligations SET state='published',receipt_message_id=?,receipt_seq=?,"
                "revision=revision+1,updated_at_ms=? WHERE execution_id=? AND revision=?",
                (
                    matched.message_id,
                    matched.seq,
                    store._now(obligation.updated_at_ms),
                    intent.execution_id,
                    obligation.revision,
                ),
            )
            db.execute(
                "UPDATE attempts SET phase='succeeded',lease_expires_at_ms=NULL,"
                "revision=revision+1,updated_at_ms=? "
                "WHERE execution_id=? AND attempt_ordinal=? AND revision=?",
                (
                    store._now(attempt.updated_at_ms),
                    intent.execution_id,
                    attempt.attempt_ordinal,
                    attempt.revision,
                ),
            )
            db.execute(
                "UPDATE executions SET status='completed',revision=revision+1,"
                "updated_at_ms=? WHERE execution_id=? AND revision=?",
                (
                    store._now(snapshot.execution.updated_at_ms),
                    intent.execution_id,
                    snapshot.execution.revision,
                ),
            )
            for claim in snapshot.claims:
                db.execute(
                    "UPDATE wake_claims SET disposition='completed',revision=revision+1,"
                    "updated_at_ms=? WHERE claim_id=? AND revision=?",
                    (store._now(claim.updated_at_ms), claim.claim_id, claim.revision),
                )
            db.execute(
                "UPDATE current_executions SET execution_id=NULL,attempt_ordinal=NULL,"
                "pointer_revision=pointer_revision+1 WHERE owner_lookup=? "
                "AND pointer_revision=? AND execution_id=?",
                (
                    snapshot.execution.owner_lookup,
                    snapshot.pointer_revision,
                    intent.execution_id,
                ),
            )
            return Applied(store._snapshot(intent.execution_id))


def publish_fenced_response(
    store: MutationStore,
    bus: MessageBus,
    fence: OwnerFence,
    *,
    owner_pid: int | None = None,
    owner_witness: LiveResponseOwner | None = None,
) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
    """Explicit one-time owner append + Tx2. Never call after an uncertain crash."""
    return _settle_fenced_response(
        store, bus, fence, allow_append=True, owner_pid=owner_pid, owner_witness=owner_witness
    )


def resolve_existing_response(
    store: MutationStore,
    bus: MessageBus,
    fence: OwnerFence,
    *,
    owner_pid: int | None = None,
    owner_witness: LiveResponseOwner | None = None,
) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
    """Read-only bus resolution of Tx1; absence never appends or retries."""
    return _settle_fenced_response(
        store, bus, fence, allow_append=False, owner_pid=owner_pid, owner_witness=owner_witness
    )
