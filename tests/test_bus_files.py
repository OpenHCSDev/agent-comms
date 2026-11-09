"""The real bus publishes only files declared by their existing state owners."""


import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from agent_comms.comms import Comms, wire
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.session import NativeCapture
from agent_comms.execution.native_input_proof import NativeContextJournal, NativeInputProof
from agent_comms.execution.native_session_reopen import PiSessionId
from agent_comms.execution.turn_item import FreshNativeId
from agent_comms.installer.backup import bus_files
from agent_comms.messaging.envelope_claim_transitions import ExistingFileClaim, NativeClaim
from agent_comms.nominal.field_codec import ClaimId, GoalId, InputId, TurnId
from agent_comms.planning.orders import TodoStore
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import (
    DeclaredFile,
    DurableFile,
    LiveFilesRequest,
    RootFile,
    RuntimeFile,
    SQLiteFile,
)
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.storage.typed_table import ReconciledFile
from bus_record_fixture import prepare_native_comms
from test_todos import order


@pytest.fixture
def comms(tmp_path: Path) -> Comms:
    root = tmp_path / "bus"
    return prepare_native_comms(wire(root))


def test_published_change_signal_is_not_a_bus_resource(tmp_path: Path) -> None:
    path = CoordinationStore.path_in(tmp_path)
    with CoordinationStore(path):
        pass
    CoordinationStore.publish_change(path)

    resources = bus_files(LiveFilesRequest(tmp_path))
    paths = {resource.path for resource in resources}
    assert path.relative_to(tmp_path) in paths
    assert all(marker.relative_to(tmp_path) not in paths
               for marker in CoordinationStore.source_paths(tmp_path))


def test_every_bus_file_is_a_declared_root_file(tmp_path: Path, source_repository, comms: Comms) -> None:
    declared_files_goal_id = GoalId.mint()
    for name in ("owner", "peer"):
        comms.registry.declare(NativeThread(name, frozenset(), Checkout(tmp_path)))
    sent = comms.messaging.send("peer", "owner", "Declare the bus files").message
    resource = tmp_path / "owned.py"
    resource.write_text("owned\n")
    with NativeCapture(comms.bus) as boundary:
        owner = boundary.registry.require("owner")
        claim = comms.bus.publisher._publish_acquisition_unlocked(
            boundary.registry,
            owner,
            ExistingFileClaim(resource).normalized(tmp_path),
            NativeClaim(
                owner_claimed_generation=boundary.registry.claim_generation(owner.name),
                operation_id=ClaimId("a" * 32),
                turn_id=TurnId('76716892c3b5bb4cc8b0f07da1976c7e'),
                input_id=InputId.mint(),
            ),
            activity=boundary.activity,
        )
    assert claim.seq > sent.seq
    todo = TodoStore(comms.root).create(
        "P1", RepositoryName.from_wire("OpenHCSDev/agent-comms"),
        "Keep declared bus files", creator=owner, assigner=owner.incarnation, owner=owner,
        generation=declared_files_goal_id,
        request=comms.goals.todos.add_request(owner.incarnation, "Keep declared bus files").id,
        review_at=RecordedEventTime(1000.0), order=order(source_repository),
    )
    assert todo.id == "P1"
    declared = RootFile.paths_in(comms.root)
    # Directory traversal observes the outcome; it is not the production inventory.
    actual = {path for path in comms.root.rglob("*") if path.is_file()}
    assert actual <= declared, sorted(str(path.relative_to(comms.root)) for path in actual - declared)
    durable = DurableFile.paths_in(comms.root)
    runtime = RuntimeFile.paths_in(comms.root)
    assert declared == durable | runtime
    assert not durable & runtime


def journal_one_context(db: sqlite3.Connection) -> None:
    """Create the native context journal and commit one context row through its owner."""
    for statement in NativeContextJournal.schema_objects().values():
        db.execute(statement)
    db.commit()
    NativeContextJournal(
        input_id=InputId.mint(), session_id=PiSessionId("session"), session_entry_id=FreshNativeId("entry"),
        request_generation=1, llm_context_digest="c" * 64,
        committed_at_ms=1_791_964_800_001, schema=2, type="context_committed",
    ).insert(db)


@pytest.mark.parametrize("journal_mode", ("DELETE", "WAL"))
def test_parameterized_sqlite_resources_follow_their_file_owner(tmp_path: Path, journal_mode: str) -> None:
    proof = NativeInputProof.path_in(tmp_path, lookup="a" * 32, name="session.jsonl")
    proof.parent.mkdir(parents=True, mode=0o700)
    with sqlite3.connect(proof) as db:
        db.execute(f"PRAGMA journal_mode={journal_mode}")
        journal_one_context(db)
        actual = {path for path in tmp_path.rglob("*") if path.is_file()}
        assert len(actual) > 1  # The real SQLite writer has an open journal/WAL.
        assert actual <= NativeInputProof.files_in(tmp_path)
        assert DeclaredFile(NativeInputProof, proof.relative_to(tmp_path)) in bus_files(LiveFilesRequest(tmp_path))


def test_installer_reconciles_a_native_proof_journal_onto_its_declaration(tmp_path: Path) -> None:
    proof = NativeInputProof.path_in(tmp_path, lookup="a" * 32, name="session")
    proof.parent.mkdir(parents=True, mode=0o700)
    with sqlite3.connect(proof) as db:
        journal_one_context(db)
        # A journal written before the trigger names moved: triggers hold no rows.
        for name in ("insert", "update", "delete"):
            db.execute(f"DROP TRIGGER native_context_journal_{name}_before_abort")
        db.execute("CREATE TRIGGER native_context_journal_update BEFORE UPDATE ON "
                   "native_context_journal BEGIN SELECT RAISE(ABORT,'append only'); END")
    assert ReconciledFile.existing_in(tmp_path) == {proof: NativeInputProof}
    assert NativeInputProof.bus_root(proof) == tmp_path
    with closing(NativeInputProof.writer(proof, isolation_level=None)) as db:
        with SQLiteFile.write_transaction(db):
            NativeContextJournal.carry_schema(db)
        assert not NativeContextJournal.schema_differs(db)
        assert db.execute("SELECT count(*) FROM native_context_journal").fetchone() == (1,)
