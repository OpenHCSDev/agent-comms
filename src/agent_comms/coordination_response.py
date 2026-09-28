"""Owner-fenced response publication across the coordinator and bus.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .bus_publication import stable_thread_lookup
from .coordination import (
    Attempts,
    CurrentExecutions,
    Executions,
    Obligations,
    OwnerFence,
    PublicationIntents,
    PublicationReceipts,
    RecoverySnapshot,
    WakeClaims,
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
from .message_bus import MessageBus
from .messages import Message, MessageType
from .registry_document import RegistrySnapshot
from .store_files import _store_lock
from .thread_identity import ThreadRole
from .turn_lease import ActiveTurn
from .typed_table import Column, SQLiteForeignKeys, SQLiteSchemaObject, TypedRow, TypedTable
from .wake import WakeDecision, derive_exact_reply_target


class ResponseTable:
    """Frozen declaration-owned response admission and append evidence."""

    @classmethod
    def triggers(cls) -> dict[str, str]:
        return {
            f"{cls.declared_name}_{operation.lower()}_guard": f"CREATE TRIGGER {cls.declared_name}_"
            f"{operation.lower()}_guard "
            f"BEFORE {operation} ON {cls.declared_name} "
            "BEGIN SELECT RAISE(ABORT,'response evidence is frozen'); END"
            for operation in ("UPDATE", "DELETE")
        }


@dataclass(frozen=True)
class ResponseSchemaMeta(ResponseTable, TypedTable):
    singleton: Literal[1] = field(metadata={"sql": Column(primary_key=True)})
    version: Literal[2]
    ddl_digest: str = field(metadata={"sql": Column(check="length(ddl_digest)=64")})


@dataclass(frozen=True)
class PublicationAppendDispatches(ResponseTable, TypedTable):
    execution_id: str = field(
        metadata={"sql": Column(primary_key=True, references=(PublicationIntents, "execution_id"))}
    )
    wire_root_id: str = field(
        metadata={
            "sql": Column(check="length(wire_root_id)=32 AND wire_root_id NOT GLOB '*[^0-9a-f]*'")
        }
    )
    owner_generation: int = field(metadata={"sql": Column(check="owner_generation>0")})
    attempt_ordinal: int = field(metadata={"sql": Column(check="attempt_ordinal>0")})
    dispatched_at_ms: int = field(metadata={"sql": Column(check="dispatched_at_ms>=0")})
    without_rowid = True

    def matches(self, wire_root_id: str, fence: OwnerFence) -> bool:
        return (self.wire_root_id, self.owner_generation, self.attempt_ordinal) == (
            wire_root_id,
            fence.owner_generation,
            fence.attempt_ordinal,
        )


@dataclass(frozen=True)
class SelectedResponseRoute(TypedRow):
    message_id: str
    exact_target: str
    envelope_digest: str
    audience_digest: str
    decisions_digest: str
    resolver_version: str
    policy_version: str
    recipient_lookup: str
    canonical_thread: str


def _response_schema() -> dict[str, str]:
    return {
        name: sql
        for table in TypedTable.members_with(ResponseTable)
        for name, sql in table.schema_objects().items()
    }


def _response_digest(schema: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(schema, separators=(",", ":")).encode()).hexdigest()


def _assert_response_schema(db: sqlite3.Connection) -> None:
    schema = _response_schema()
    tables = tuple(table.declared_name for table in TypedTable.members_with(ResponseTable))
    try:
        meta = ResponseSchemaMeta.one(db, singleton=1)
        actual = SQLiteSchemaObject.read(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL AND tbl_name IN ("
                + ",".join("?" for _ in tables)
                + ")",
                tables,
            )
        )
    except (sqlite3.Error, ValueError, TypeError) as error:
        raise PublicationActivationBlocked("private response schema is not installed") from error
    if meta != ResponseSchemaMeta(1, 2, _response_digest(schema)):
        raise PublicationActivationBlocked("private response schema version is unsupported")
    if {row.name: row.sql for row in actual} != schema or SQLiteForeignKeys.read(
        db.execute("PRAGMA foreign_keys")
    ) != [SQLiteForeignKeys(True)]:
        raise PublicationActivationBlocked("private response schema has drifted")


def install_private_response_schema(store: MutationStore) -> None:
    """Install the current response tables on a fresh coordinator only."""
    if type(store) is not MutationStore:
        raise TypeError("response schema requires the actual coordinator store")
    with store._transaction() as db:
        exists = SQLiteSchemaObject.read(
            db.execute(
                "SELECT name,sql FROM sqlite_master WHERE name=?",
                (ResponseSchemaMeta.declared_name,),
            )
        )
        if not exists:
            schema = _response_schema()
            for statement in schema.values():
                db.execute(statement)
            ResponseSchemaMeta(1, 2, _response_digest(schema)).insert(db)
        _assert_response_schema(db)


@contextmanager
def _response_boundary(bus: MessageBus, *, blocking: bool = True) -> Iterator[RegistrySnapshot]:
    """Total lock order: shared wire -> bus -> registry -> SQLite.

    Raw keyed appends acquire bus then registry; Comms register takes shared
    wire then registry. No registry API that reacquires its lock may be used in
    this boundary: the bus append receives this immutable loaded revision.
    """
    with (
        _store_lock(bus.log.path.parent / "wire", blocking=blocking),
        bus.log.locked(blocking=blocking),
        _store_lock(bus._registry.store.path, blocking=blocking),
    ):
        yield bus._registry.store._read_unlocked().snapshot()


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

    def require_live(self, snapshot: RegistrySnapshot, fence: OwnerFence) -> None:
        """Recheck this exact process/turn against current registry authority."""
        if (
            self.name != fence.owner_thread
            or type(self.active_turn) is not ActiveTurn
            or self.active_turn.owner_pid != self.pid
            or self.active_turn.admission_generation != self.admission_generation
            or type(self.admission_generation) is not int
            or self.admission_generation < 1
            or type(self.pid) is not int
        ):
            raise StaleFence("response turn witness is invalid")
        if self.pid != os.getpid():
            raise StaleFence("response witness does not own the current process")
        thread = snapshot.threads.get(fence.owner_thread)
        status = snapshot.statuses.get(fence.owner_thread)
        if (
            thread is None
            or status is None
            or not status.active
            or thread.role is not ThreadRole.AGENT
            or thread.name != fence.owner_thread
            or thread.pid != self.pid
            or thread.created_at != self.created_at
            or stable_thread_lookup(thread.created_at) != self.recipient_lookup
            or thread.worktree != self.worktree
            or thread.active_turn != self.active_turn
            or snapshot.admission_generations.get(thread.name) != self.admission_generation
        ):
            raise StaleFence("response owner turn stopped or changed before publication")


def _require_bound_stores(bus: MessageBus, store: MutationStore) -> None:
    if type(bus) is not MessageBus or type(store) is not MutationStore:
        raise TypeError("response requires real, explicitly gated bus and coordinator stores")
    if bus.publisher._private_response_writes is not True:
        raise PublicationActivationBlocked("private response publication is disabled")
    if (
        store.path.name != "coordination.sqlite3"
        or Path(bus.log.path.parent).absolute() != Path(store.path.parent).absolute()
        or bus._registry.store.path.absolute() != (bus.log.path.parent / "registry.json").absolute()
    ):
        raise IdentityConflict("response bus and coordinator have different trusted roots")


def _require_cohort_assignments(
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
    if not snapshot.assignments or snapshot.obligation is None:
        raise IdentityConflict("wire response requires selected claims and obligation")
    metadata = bus.log._private_marker_unlocked()
    if metadata.root_id != wire_root_id:
        raise IdentityConflict("cohort bus root changed")
    originals = {
        message.seq: initial
        for message, _receipt, initial in bus.log._verified_private_rows_unlocked(metadata)
        if initial is not None
    }
    for assignment in snapshot.assignments:
        initial = originals.get(assignment.wire_seq)
        receipts = SelectedResponseRoute.read(
            db.execute(
                "SELECT r.message_id,r.exact_target,r.envelope_digest,r.audience_digest,"
                "r.decisions_digest,r.resolver_version,r.policy_version,m.recipient_lookup,"
                "d.canonical_thread FROM claim_batch_members m JOIN claim_batch_receipts r "
                "ON r.wire_root_id=m.wire_root_id AND r.wire_seq=m.wire_seq "
                "JOIN cohort_delivery_receipts d "
                "ON d.wire_root_id=m.wire_root_id AND d.wire_seq=m.wire_seq "
                "AND d.claim_id=m.claim_id AND d.kind='selected' "
                "WHERE m.claim_id=? AND m.wire_root_id=? AND r.sealed=1",
                (assignment.assignment_id, wire_root_id),
            )
        )
        if initial is None or len(receipts) != 1:
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
            or assignment.message_id != initial.message.message_id
            or receipts[0]
            != SelectedResponseRoute(
                initial.message.message_id,
                initial.message.target,
                initial.audience.wire_envelope_digest,
                initial.audience.digest,
                initial.decisions_digest,
                assignment.resolver_version,
                assignment.policy_version,
                assignment.recipient_lookup,
                assignment.recipient,
            )
            or assignment.recipient_lookup not in selected
            or derive_exact_reply_target(initial.message) != snapshot.execution.exact_target
            or assignment.lifecycle.exact_target != snapshot.execution.exact_target
            or not (assignment.lifecycle.completed if terminal else assignment.lifecycle.engaged)
        ):
            raise IdentityConflict("response claim conflicts with original selected bus route")


def _require_final_owner(
    store: MutationStore,
    bus: MessageBus,
    fence: OwnerFence,
    wire_root_id: str,
    owner_witness: LiveResponseOwner,
) -> RecoverySnapshot:
    snapshot, attempt = store._assert_fence(fence)
    execution = snapshot.execution
    if execution.owner_lookup != owner_witness.recipient_lookup:
        raise StaleFence("response turn belongs to a different SQL recipient")
    if (
        not execution.lifecycle.active
        or not attempt.lifecycle.settling
        or not (attempt.lifecycle.backend_done and attempt.lifecycle.process_dead)
        or snapshot.obligation is None
        or snapshot.obligation.exact_target != execution.exact_target
        or execution.exact_target is None
    ):
        raise RecoveryBlocked("response requires final model/death evidence and exact wire route")
    _require_cohort_assignments(store, bus, snapshot, wire_root_id)
    return snapshot


def _intent_matches_request(
    intent: PublicationIntents,
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
    owner_witness: LiveResponseOwner,
) -> Applied[PublicationIntents] | AlreadyApplied[PublicationIntents]:
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
        owner_witness.require_live(registry_snapshot, fence)
        metadata = bus.log._private_marker_unlocked()
        # A corrupt row anywhere is never accepted as an absent publication.
        tuple(bus.log._verified_private_rows_unlocked(metadata))
        with store._transaction() as db:
            snapshot = _require_final_owner(store, bus, fence, metadata.root_id, owner_witness)
            execution = snapshot.execution
            assert execution.exact_target is not None
            existing = snapshot.publication_intent
            if existing is not None:
                if (
                    snapshot.obligation is None
                    or not snapshot.obligation.lifecycle.publishing
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
            if snapshot.obligation is None or not snapshot.obligation.lifecycle.pending:
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
            intent = PublicationIntents(
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
            intent.insert(db)
            Obligations.update(
                db,
                where="execution_id=? AND state='pending' AND revision=?",
                parameters=(execution.execution_id, snapshot.obligation.revision),
                state="publishing",
                revision=snapshot.obligation.revision + 1,
                updated_at_ms=store._now(snapshot.obligation.updated_at_ms),
            )
            return Applied(intent)


def _terminal_replay(
    store: MutationStore,
    fence: OwnerFence,
    bus: MessageBus,
    wire_root_id: str,
    owner_witness: LiveResponseOwner,
) -> AlreadyApplied[RecoverySnapshot] | None:
    """A finished immutable receipt may be re-read, never re-published."""
    snapshot = store.snapshot(fence.execution_id)
    if not snapshot.execution.lifecycle.completed:
        return None
    if snapshot.execution.owner_lookup != owner_witness.recipient_lookup:
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
    _require_cohort_assignments(store, bus, snapshot, wire_root_id, terminal=True)
    matched, _, _ = bus.log._keyed_receipt_unlocked(snapshot.publication_intent)
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
    owner_witness: LiveResponseOwner,
) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
    _require_bound_stores(bus, store)
    if type(fence) is not OwnerFence:
        raise TypeError("response settlement requires an exact owner fence")
    # One registry revision is held through dispatch, append and Tx2. SQL's
    # current owner generation remains guarded by the transaction fence.
    with _response_boundary(bus) as registry_snapshot:
        owner_witness.require_live(registry_snapshot, fence)
        metadata = bus.log._private_marker_unlocked()
        wire_root_id = metadata.root_id
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
                    or not snapshot.obligation.lifecycle.publishing
                    or snapshot.publication_intent.sender != snapshot.execution.owner_thread
                    or snapshot.publication_intent.exact_target != snapshot.execution.exact_target
                ):
                    raise IdentityConflict("no frozen publishing intent for current owner")
                prior = PublicationAppendDispatches.one(db, execution_id=fence.execution_id)
                if prior is None:
                    PublicationAppendDispatches(
                        fence.execution_id,
                        wire_root_id,
                        fence.owner_generation,
                        fence.attempt_ordinal,
                        store._now(snapshot.execution.updated_at_ms),
                    ).insert(db)
                    first_dispatch = True
                elif not prior.matches(wire_root_id, fence):
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
                or not snapshot.obligation.lifecycle.publishing
                or intent.sender != snapshot.execution.owner_thread
                or intent.exact_target != snapshot.execution.exact_target
            ):
                raise IdentityConflict("no frozen publishing intent for current owner")
            dispatch = PublicationAppendDispatches.one(db, execution_id=fence.execution_id)
            if dispatch is None or not dispatch.matches(wire_root_id, fence):
                raise PublicationUncertain("no matching durable append dispatch")
            matched, _, _ = bus.log._keyed_receipt_unlocked(intent)
            if matched is None:
                if not first_dispatch:
                    raise PublicationUncertain("no keyed bus receipt; no resend authorized")
                # This call created the durable dispatch barrier, and owns the
                # only authorized first append while its final fence is locked.
                matched = bus.publisher._publish_keyed_response_unlocked(
                    intent, registry_snapshot=registry_snapshot
                )
            if matched.message_id != intent.expected_message_id:
                raise PublicationUncertain("bus receipt conflicts with immutable intent")
            now = store._now(max(snapshot.execution.updated_at_ms, attempt.updated_at_ms))
            PublicationReceipts(
                execution_id=intent.execution_id,
                seq=matched.seq,
                message_id=matched.message_id,
                received_at_ms=now,
            ).insert(db)
            obligation = snapshot.obligation
            Obligations.update(
                db,
                where="execution_id=? AND revision=?",
                parameters=(intent.execution_id, obligation.revision),
                state="published",
                receipt_message_id=matched.message_id,
                receipt_seq=matched.seq,
                revision=obligation.revision + 1,
                updated_at_ms=store._now(obligation.updated_at_ms),
            )
            Attempts.update(
                db,
                where="execution_id=? AND attempt_ordinal=? AND revision=?",
                parameters=(intent.execution_id, attempt.attempt_ordinal, attempt.revision),
                phase="succeeded",
                lease_expires_at_ms=None,
                revision=attempt.revision + 1,
                updated_at_ms=store._now(attempt.updated_at_ms),
            )
            Executions.update(
                db,
                where="execution_id=? AND revision=?",
                parameters=(intent.execution_id, snapshot.execution.revision),
                status="completed",
                revision=snapshot.execution.revision + 1,
                updated_at_ms=store._now(snapshot.execution.updated_at_ms),
            )
            for assignment in snapshot.assignments:
                WakeClaims.update(
                    db,
                    where="claim_id=? AND revision=?",
                    parameters=(assignment.assignment_id, assignment.revision),
                    disposition="completed",
                    revision=assignment.revision + 1,
                    updated_at_ms=store._now(assignment.updated_at_ms),
                )
            CurrentExecutions.update(
                db,
                where="owner_lookup=? AND pointer_revision=? AND execution_id=?",
                parameters=(
                    snapshot.execution.owner_lookup,
                    snapshot.pointer_revision,
                    intent.execution_id,
                ),
                execution_id=None,
                attempt_ordinal=None,
                pointer_revision=snapshot.pointer_revision + 1,
            )
            return Applied(store._snapshot(intent.execution_id))


def publish_fenced_response(
    store: MutationStore,
    bus: MessageBus,
    fence: OwnerFence,
    *,
    owner_witness: LiveResponseOwner,
) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
    """Explicit one-time owner append + Tx2. Never call after an uncertain crash."""
    return _settle_fenced_response(
        store, bus, fence, allow_append=True, owner_witness=owner_witness
    )


def resolve_existing_response(
    store: MutationStore,
    bus: MessageBus,
    fence: OwnerFence,
    *,
    owner_witness: LiveResponseOwner,
) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
    """Read-only bus resolution of Tx1; absence never appends or retries."""
    return _settle_fenced_response(
        store, bus, fence, allow_append=False, owner_witness=owner_witness
    )
