"""Resource ownership, originals and derived rows share the actual Bus transaction."""

import os
import signal
import sqlite3
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

import pytest

from agent_comms.bus import Bus, ClaimRecord, KeyedPrivateRecord, PublicRecord, RecordBuilder
from agent_comms.bus_publication import HumanOrigin
from agent_comms.messages import (
    BusProjectionTables,
    BusRow,
    BusTables,
    ClaimOperations,
    HeldResources,
    ReplayedHolders,
    Claimed,
    ClaimNoticeKind,
    InfoKind,
    Message,
)
from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.envelope_claim_transitions import (
    ClaimConflict, ClaimOwner, ClaimRelease, NativeClaim, ReleasedClaims,
    UnclaimedResource, WakeClaims,
)
from agent_comms.errors import CommittedMessageError
from agent_comms.locked_store import RegistryFile
from agent_comms.registration import Registration
from agent_comms.store_files import store_lock_path
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.locked_store import StoreCommittedError
from agent_comms.typed_table import TypedTable
from bus_record_fixture import publication_processes, record_from


@dataclass(frozen=True)
class ClaimBus:
    bus: Bus
    registry: Registration

    def scope(self, name: str, operation: str) -> NativeClaim:
        return NativeClaim(
            owner_claimed_generation=self.registry.snapshot().claim_generation(name),
            operation_id=operation, turn_id=f"turn-{name}", input_id=f"input-{name}",
        )

    def builder(self, name: str, transition) -> RecordBuilder:
        registry = self.registry.snapshot()
        owner = registry.threads[registry.canonical_name(name)]

        def build(seq: int, db: sqlite3.Connection) -> ClaimRecord:
            message = replace(ClaimNoticeKind.claimed(owner), seq=seq)
            prepared = self.bus.prepare_claim(db, transition(
                owner=name, incarnation=str(owner.created_at), seq=seq,
                message_id=message.message_id,
            ), registry)
            return ClaimRecord.of(message, prepared)

        return build

    def acquisition(self, name: str, resources: tuple[str, ...], scope: NativeClaim) -> RecordBuilder:
        return self.builder(name, partial(
            WakeClaims, claims=resources, generation=scope.operation_id, resource_scope=scope,
        ))

    def acquire(self, name: str, resource: str, scope: NativeClaim) -> ClaimOwner:
        return self.bus.acquire_resource_claim(
            resource, scope, self.registry.snapshot(), self.acquisition(name, (resource,), scope),
        )

    def release(self, owner: ClaimOwner) -> None:
        self.bus.append(self.builder(owner.owner, partial(
            ReleasedClaims, releases=(ClaimRelease(owner.resource, owner.generation),),
        )))

    def counts(self) -> dict[type[TypedTable], int]:
        with self.bus.observing() as db:
            return {table: table.count(db) for group in (BusTables, BusProjectionTables)
                    for table in TypedTable.members_with(group)}


@pytest.fixture
def claims(tmp_path: Path) -> ClaimBus:
    root = tmp_path / "bus"
    registry = Registration(RegistryFile.path_in(root))
    for name in ("alice", "bob"):
        registry.declare(registry.new_thread(
            name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()),
        ), RunningThreadStatus())
    bus = Bus.install(Bus.path_in(root))
    with bus.writing():
        pass
    return ClaimBus(bus, registry)


def test_existing_owner_allocates_nothing_and_never_invokes_builder(claims: ClaimBus, tmp_path: Path):
    resource = str(tmp_path / "owned.py")
    scope = claims.scope("alice", "a" * 32)
    owner = claims.acquire("alice", resource, scope)
    before = claims.counts()
    signal = Bus.source_paths(claims.bus.path.parent)[0].stat().st_mtime_ns

    def unused(seq, db):
        raise AssertionError("same-owner acquisition invoked its builder")

    assert claims.bus.acquire_resource_claim(resource, scope, claims.registry.snapshot(), unused) == owner
    with claims.bus.observing() as db:
        assert BusRow.next_seq(db) == owner.seq + 1
        assert claims.bus.resource_claim(db, resource) == owner
    with claims.bus.reading() as reader:
        assert reader.resource_owner(resource) == owner
        assert dict(reader.claim_projection()) == {resource: owner}
    from agent_comms.field_codec import FieldCodec
    from agent_comms.retained_task_facts import ClaimTaskFact

    record = claims.bus.lookup(owner.seq)
    fact, = record.retained_task_facts()
    assert fact.source.claim_transition == record.require_claim_transition()
    assert FieldCodec.decode(ClaimTaskFact, FieldCodec.encode(fact)) == fact
    assert claims.counts() == before
    assert Bus.source_paths(claims.bus.path.parent)[0].stat().st_mtime_ns == signal


def test_conflict_release_and_consumed_operation_share_the_original(claims: ClaimBus, tmp_path: Path):
    resource = str(tmp_path / "owned.py")
    scope = claims.scope("alice", "a" * 32)
    owner = claims.acquire("alice", resource, scope)
    before = claims.counts()
    with pytest.raises(ClaimConflict) as lost:
        claims.acquire("bob", resource, claims.scope("bob", "b" * 32))
    assert lost.value.existing == owner
    assert claims.counts() == before
    claims.release(owner)
    assert claims.bus.resource_owner(resource) == UnclaimedResource()
    with claims.bus.observing() as db:
        assert ClaimOperations.used(db, scope.operation_id)
        assert Claimed.table.require(db, seq=owner.seq).claim_transition.claimed(resource) == owner
    with pytest.raises(IdentityConflict):
        claims.acquire("alice", resource, scope)
    successor = claims.acquire("bob", resource, claims.scope("bob", "c" * 32))
    assert successor.owner == "bob" and successor.seq > owner.seq
    assert dict(claims.bus.claim_projection()) == {resource: successor}


def test_conflicting_set_does_not_acquire_its_uncontested_resource(claims: ClaimBus, tmp_path: Path):
    resources = tuple(str(tmp_path / name) for name in ("a.py", "b.py"))
    held = claims.acquire("bob", resources[1], claims.scope("bob", "a" * 32))
    scope = claims.scope("alice", "b" * 32)
    before = claims.counts()
    with pytest.raises(ClaimConflict):
        claims.bus.append(claims.acquisition("alice", resources, scope))
    assert claims.counts() == before
    assert claims.bus.resource_owner(resources[0]) == UnclaimedResource()
    assert claims.bus.resource_owner(resources[1]) == held
    claims.release(held)
    acquired = claims.bus.append(claims.acquisition("alice", resources, scope))
    assert acquired.claim_transition.claims == resources
    assert set(claims.bus.claim_projection()) == set(resources)


def test_claim_then_keyed_response_share_original_sequence_and_identity(claims: ClaimBus, tmp_path: Path):
    owner = claims.acquire("alice", str(tmp_path / "owned.py"), claims.scope("alice", "a" * 32))
    original = claims.bus.lookup(owner.seq)
    response = claims.bus.append(lambda seq, db: record_from(
        KeyedPrivateRecord,
        Message("bob", "alice", "done", InfoKind, seq=seq, answers=(original.reference,)),
    ))
    assert response.seq == original.seq + 1
    assert claims.bus.messages_for_references((original.reference, response.reference)) == (original, response)
    assert claims.bus.replies((original.reference,)) == (response,)
    assert claims.bus.lookup(owner.seq).require_claim_transition().claimed(owner.resource) == owner


def test_expiry_and_rebuild_use_claimed_replay_not_a_checkpoint(claims: ClaimBus, tmp_path: Path):
    stale = tuple(str(tmp_path / name) for name in ("a.py", "b.py"))
    scope = claims.scope("alice", "a" * 32)
    claims.bus.append(claims.acquisition("alice", stale, scope))
    claims.registry.register(replace(
        claims.registry.require("alice"), process_identity=NoProcess(),
    ), StoppedThreadStatus())
    resource = str(tmp_path / "c.py")
    current = claims.acquire("bob", resource, claims.scope("bob", "b" * 32))
    assert set(claims.bus.claim(current.seq).expired) == set(stale)
    claims.release(current)
    claims.acquire("bob", resource, claims.scope("bob", "c" * 32))
    before = claims.bus.claim_projection()
    counts = claims.counts()
    with claims.bus.writing() as db:
        HeldResources.delete(db)
        ClaimOperations.delete(db)
        holders = ReplayedHolders()
        for original in Claimed.table.select(db, order_by=("seq",)):
            Claimed(claim_transition=original.claim_transition).project_into(db, holders)
    assert claims.bus.claim_projection() == before
    assert claims.counts() == counts


def test_failed_projection_rolls_back_original_operations_and_sequence(claims: ClaimBus, tmp_path: Path):
    resource = str(tmp_path / "owned.py")
    scope = claims.scope("alice", "a" * 32)
    build = claims.acquisition("alice", (resource,), scope)
    before = claims.counts()

    def refused(seq, db):
        db.execute(f'CREATE TEMP TRIGGER refuse_claim BEFORE INSERT ON "{HeldResources.declared_name}" '
                   "BEGIN SELECT RAISE(ABORT, 'projection refused'); END")
        return build(seq, db)

    with pytest.raises(sqlite3.IntegrityError, match="projection refused"):
        claims.bus.acquire_resource_claim(resource, scope, claims.registry.snapshot(), refused)
    assert claims.counts() == before
    assert claims.bus.resource_owner(resource) == UnclaimedResource()
    assert claims.acquire("alice", resource, scope).seq == 1


def test_admitted_snapshot_keeps_its_claim_while_another_writer_releases(claims: ClaimBus, tmp_path: Path):
    resource = str(tmp_path / "owned.py")
    owner = claims.acquire("alice", resource, claims.scope("alice", "a" * 32))
    with claims.bus.reading() as snapshot:
        assert snapshot.resource_owner(resource) == owner
        claims.release(owner)
        assert snapshot.resource_owner(resource) == owner
        assert dict(snapshot.claim_projection()) == {resource: owner}
    assert claims.bus.resource_owner(resource) == UnclaimedResource()


def append_until_projected(root: Path, scope: NativeClaim, resource: str, ready) -> None:
    claims = ClaimBus(Bus(Bus.path_in(root)), Registration(RegistryFile.path_in(root)))

    def projected():
        ready.send("original, held resource and operation inserted")
        os.kill(os.getpid(), signal.SIGSTOP)
        return 0

    build = claims.acquisition("alice", (resource,), scope)

    def stopped(seq, db):
        db.create_function("projected", 0, projected)
        db.execute(f'CREATE TEMP TRIGGER stop_projected AFTER INSERT ON "{ClaimOperations.declared_name}" '
                   "BEGIN SELECT projected(); END")
        return build(seq, db)

    claims.bus.acquire_resource_claim(resource, scope, claims.registry.snapshot(), stopped)


def test_sigkill_after_projection_leaves_no_original_or_derived_claim(claims: ClaimBus, tmp_path: Path):
    before = claims.counts()
    resource = str(tmp_path / "owned.py")
    scope = claims.scope("alice", "a" * 32)
    reader, writer = publication_processes.Pipe(duplex=False)
    child = publication_processes.Process(target=append_until_projected,
                            args=(claims.bus.path.parent, scope, resource, writer))
    child.start()
    writer.close()
    try:
        assert reader.poll(10), "child did not finish its original and projection inserts"
        assert reader.recv() == "original, held resource and operation inserted"
        os.kill(child.pid, signal.SIGKILL)
        child.join(10)
        assert child.exitcode == -signal.SIGKILL
        assert claims.counts() == before
        assert claims.bus.resource_owner(resource) == UnclaimedResource()
        assert claims.acquire("alice", resource, scope).seq == 1
    finally:
        reader.close()
        if child.is_alive():
            child.kill()
            child.join(10)
        child.close()
    print("SIGKILL after original+resource+operation inserts: every table unchanged; next seq=1")


def test_retirement_failure_is_sent_not_a_refusal(tmp_path: Path):
    bus = Bus.install(Bus.path_in(tmp_path / "bus"))
    bus.append(lambda seq, db: PublicRecord.from_message(
        Message("author", "worker", "warm", InfoKind, seq=seq),
    ))
    connections = []

    def build(seq, db):
        connections.append(db)
        store_lock_path(bus.path).unlink()
        bus.path.parent.chmod(0o500)
        return PublicRecord.from_message(Message("author", "worker", "sent", InfoKind, seq=seq))

    try:
        with pytest.raises(CommittedMessageError) as failed:
            HumanOrigin("author", 1, str(tmp_path)).append(bus, build)
    finally:
        bus.path.parent.chmod(0o700)
        for connection in connections:
            connection.close()
    assert isinstance(failed.value.__cause__, StoreCommittedError)
    assert isinstance(failed.value.__cause__.__cause__, PermissionError)
    assert failed.value.record.body == "sent"
    assert bus.lookup(failed.value.record.seq) == failed.value.record
    print("Post-COMMIT lock-retirement PermissionError: human outcome is sent, exact record retained")
