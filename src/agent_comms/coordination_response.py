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
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agent_comms.attempt_states import SucceededAttempt
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_schema import assert_cohort_schema
from agent_comms.coordination_errors import (
    IdentityConflict,
    PublicationActivationBlocked,
    PublicationUncertain,
    RecoveryBlocked,
    StaleFence,
)
from agent_comms.coordination_results import AlreadyApplied, Applied
from agent_comms.coordination_snapshot import RecoverySnapshot
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordination_tables.attempts import AttemptRecord
from agent_comms.coordination_tables.executions import CurrentExecutions, ExecutionRecord
from agent_comms.coordination_tables.publications import (
    PublicationIntents,
    PublicationReceipts,
    canonical_publication_key,
)
from agent_comms.coordination_tables.responses import ResponseObligation
from agent_comms.coordinator import Coordination
from agent_comms.execution_states import CompletedExecution
from agent_comms.message_bus import MessageBus
from agent_comms.messages import Message, MessageType
from agent_comms.native_admission_rules import RegistryAdmissionCheck
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.obligation_states import PublishedResponse, PublishingResponse
from agent_comms.owner_fence import OwnerFence
from agent_comms.private_runtime_schema import PrivateRuntimeSchema
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.store_files import _store_lock
from agent_comms.typed_table import (
    Column,
    SQLiteForeignKeys,
    SQLiteSchemaObject,
    TypedRow,
    TypedTable,
)
from agent_comms.wake import WakeDecision, derive_exact_reply_target


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
class ResponseSchemaMeta(ResponseTable, TypedTable, PrivateRuntimeSchema):
    @classmethod
    def install(cls, store) -> None:
        install_private_response_schema(store)

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
        return self == PublicationAppendDispatches(
            execution_id=fence.execution_id, wire_root_id=wire_root_id,
            owner_generation=fence.owner_generation, attempt_ordinal=fence.attempt_ordinal,
            dispatched_at_ms=self.dispatched_at_ms,
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

    def require_source(self, initial, assignment: WakeAssignment) -> None:
        expected = SelectedResponseRoute(
            initial.message.message_id, initial.message.target,
            initial.audience.wire_envelope_digest, initial.audience.digest,
            initial.decisions_digest, assignment.resolver_version,
            assignment.policy_version, assignment.recipient_lookup, assignment.recipient,
        )
        if self != expected:
            raise IdentityConflict("response sealed route differs from committed source")


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


def install_private_response_schema(store: Coordination) -> None:
    """Install the current response tables on a fresh coordinator only."""
    if type(store) is not Coordination:
        raise TypeError("response schema requires the actual coordinator store")
    with store.session.transaction() as db:
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


@dataclass(frozen=True, kw_only=True)
class LiveResponseOwner(RegistryOwner):
    """One captured registry owner; response finality does not depend on its goal."""

    check_type = RegistryAdmissionCheck

    @property
    def recipient_lookup(self) -> str:
        return stable_thread_lookup(self.thread.created_at)

    def require_live(self, snapshot: RegistrySnapshot, fence: OwnerFence) -> None:
        self.require_snapshot(snapshot, "response owner turn stopped or changed before publication")
        if self.thread.name != fence.owner_thread:
            raise StaleFence("response turn belongs to a different fenced owner")
        self.require_active_turn()


def _require_bound_stores(bus: MessageBus, store: Coordination) -> None:
    if type(bus) is not MessageBus or type(store) is not Coordination:
        raise TypeError("response requires real, explicitly gated bus and coordinator stores")
    if bus.publisher._private_response_writes is not True:
        raise PublicationActivationBlocked("private response publication is disabled")
    if (
        store.session.path.name != "coordination.sqlite3"
        or Path(bus.log.path.parent).absolute() != Path(store.session.path.parent).absolute()
        or bus._registry.store.path.absolute() != (bus.log.path.parent / "registry.json").absolute()
    ):
        raise IdentityConflict("response bus and coordinator have different trusted roots")


def _require_cohort_assignments(
    store: Coordination,
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
    db = store.session._connection
    _assert_response_schema(db)
    assert_cohort_schema(db)
    if not snapshot.assignments or snapshot.obligation is None:
        raise IdentityConflict("wire response requires selected claims and obligation")
    metadata = bus.log._private_marker_unlocked()
    if metadata.root_id != wire_root_id:
        raise IdentityConflict("cohort bus root changed")
    originals = {
        initial.message.seq: initial
        for record in bus.log.verified_records_unlocked(metadata)
        for initial in record.deliveries()
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
        if initial.wire_root_id != wire_root_id or assignment.source != initial.message.reference:
            raise IdentityConflict("response claim conflicts with original bus source")
        receipts[0].require_source(initial, assignment)
        if assignment.recipient_lookup not in selected:
            raise IdentityConflict("response recipient was not selected by the original source")
        engagement = (
            assignment.lifecycle.require_completion() if terminal
            else assignment.lifecycle.require_engagement()
        )
        target = snapshot.execution.exact_target
        if engagement.exact_target != target or derive_exact_reply_target(initial.message) != target:
            raise IdentityConflict("response claim conflicts with original selected bus route")



def _require_final_owner(
    store: Coordination,
    bus: MessageBus,
    fence: OwnerFence,
    wire_root_id: str,
    owner_witness: LiveResponseOwner,
) -> RecoverySnapshot:
    snapshot, attempt = store.attempts.require_fence(fence)
    execution = snapshot.execution
    if execution.owner_lookup != owner_witness.recipient_lookup:
        raise StaleFence("response turn belongs to a different SQL recipient")
    if (
        not execution.lifecycle.active
        or not attempt.lifecycle.publication_ready
        or snapshot.obligation is None
        or snapshot.obligation.exact_target != execution.exact_target
        or execution.exact_target is None
    ):
        raise RecoveryBlocked("response requires final model/death evidence and exact wire route")
    _require_cohort_assignments(store, bus, snapshot, wire_root_id)
    return snapshot


def prepare_fenced_response(
    store: Coordination,
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
        tuple(bus.log.verified_records_unlocked(metadata))
        with store.session.transaction() as db:
            snapshot = _require_final_owner(store, bus, fence, metadata.root_id, owner_witness)
            execution = snapshot.execution
            assert execution.exact_target is not None
            existing = snapshot.publication_intent
            if existing is not None:
                if (
                    snapshot.obligation is None
                    or not snapshot.obligation.lifecycle.publishing
                    or not existing.matches_request(
                        execution.execution_id,
                        Message(
                            execution.owner_thread, execution.exact_target, payload, message_type,
                            timestamp=existing.timestamp if timestamp is None else timestamp,
                            notice=notice,
                        ),
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
            ResponseObligation.update(
                db,
                where="execution_id=? AND state='pending' AND revision=?",
                parameters=(execution.execution_id, snapshot.obligation.revision),
                lifecycle=PublishingResponse(),
                revision=snapshot.obligation.revision + 1,
                updated_at_ms=store.session.now(snapshot.obligation.updated_at_ms),
            )
            return Applied(intent)


def _terminal_replay(
    store: Coordination,
    fence: OwnerFence,
    bus: MessageBus,
    wire_root_id: str,
    owner_witness: LiveResponseOwner,
) -> AlreadyApplied[RecoverySnapshot] | None:
    """A finished immutable receipt may be re-read, never re-published."""
    snapshot = store.snapshots.get(fence.execution_id)
    if not snapshot.execution.lifecycle.completed:
        return None
    if snapshot.execution.owner_lookup != owner_witness.recipient_lookup:
        raise StaleFence("finished response belongs to a different SQL recipient")
    attempt = snapshot.require_attempt()
    if attempt.authority != fence.authority:
        raise StaleFence("finished response is not this owner's original attempt")
    if snapshot.publication_intent is None or snapshot.publication_receipt is None:
        raise StaleFence("finished response has no frozen publication evidence")
    _require_cohort_assignments(store, bus, snapshot, wire_root_id, terminal=True)
    matched, _, _ = bus.log._keyed_receipt_unlocked(snapshot.publication_intent)
    if matched is None or matched.reference != snapshot.publication_receipt.reference:
        raise PublicationUncertain("published SQL receipt has no exact durable bus row")
    return AlreadyApplied(snapshot)


def _settle_fenced_response(
    store: Coordination,
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
            with store.session.transaction() as db:
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
                        store.session.now(snapshot.execution.updated_at_ms),
                    ).insert(db)
                    first_dispatch = True
                elif not prior.matches(wire_root_id, fence):
                    raise IdentityConflict("response dispatch belongs to another root or owner")
        with store.session.transaction() as db:
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
                from .response_conversation import ResponseConversation

                conversation = ResponseConversation.capture(bus, snapshot)
                matched = bus.publisher._publish_keyed_response_unlocked(
                    intent, conversation=conversation, registry_snapshot=registry_snapshot
                )
            now = store.session.now(max(snapshot.execution.updated_at_ms, attempt.updated_at_ms))
            PublicationReceipts(
                execution_id=intent.execution_id,
                seq=matched.seq,
                message_id=matched.message_id,
                received_at_ms=now,
            ).insert(db)
            obligation = snapshot.obligation
            ResponseObligation.update(
                db,
                where="execution_id=? AND revision=?",
                parameters=(intent.execution_id, obligation.revision),
                lifecycle=PublishedResponse(matched.message_id, matched.seq),
                revision=obligation.revision + 1,
                updated_at_ms=store.session.now(obligation.updated_at_ms),
            )
            AttemptRecord.update(
                db,
                where="execution_id=? AND attempt_ordinal=? AND revision=?",
                parameters=(intent.execution_id, attempt.attempt_ordinal, attempt.revision),
                lifecycle=SucceededAttempt(),
                revision=attempt.revision + 1,
                updated_at_ms=store.session.now(attempt.updated_at_ms),
            )
            ExecutionRecord.update(
                db,
                where="execution_id=? AND revision=?",
                parameters=(intent.execution_id, snapshot.execution.revision),
                lifecycle=CompletedExecution(attempt.attempt_ordinal),
                revision=snapshot.execution.revision + 1,
                updated_at_ms=store.session.now(snapshot.execution.updated_at_ms),
            )
            for assignment in snapshot.assignments:
                WakeAssignment.update(
                    db,
                    where="assignment_id=? AND revision=?",
                    parameters=(assignment.assignment_id, assignment.revision),
                    lifecycle=CompletedExecution.assignment_state().build(
                        assignment.lifecycle.mode,
                        assignment.lifecycle.execution_id,
                        assignment.lifecycle.exact_target,
                    ),
                    revision=assignment.revision + 1,
                    updated_at_ms=store.session.now(assignment.updated_at_ms),
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
            return Applied(store.snapshots.get(intent.execution_id))


def publish_fenced_response(
    store: Coordination,
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
    store: Coordination,
    bus: MessageBus,
    fence: OwnerFence,
    *,
    owner_witness: LiveResponseOwner,
) -> Applied[RecoverySnapshot] | AlreadyApplied[RecoverySnapshot]:
    """Read-only bus resolution of Tx1; absence never appends or retries."""
    return _settle_fenced_response(
        store, bus, fence, allow_append=False, owner_witness=owner_witness
    )
