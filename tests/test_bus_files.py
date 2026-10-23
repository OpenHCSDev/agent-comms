"""The real bus publishes only files declared by their existing state owners."""

from pathlib import Path
import sqlite3

import pytest

from agent_comms.installer.backup import bus_files
from bus_record_fixture import prepare_native_comms
from agent_comms.native_pi import NativeContextJournal
from agent_comms.native_session_files import NativeInputProof
from agent_comms.comms import Comms
from agent_comms.coordination_session import NativeBoundary
from agent_comms.envelope_claim_transitions import ExistingFileClaim, NativeClaim
from agent_comms.locked_store import DurableFile, RootFile, RuntimeFile
from agent_comms.private_registry_guard import PrivateOwnerRenameIntent
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import TodoStore
from test_todos import order


@pytest.fixture
def comms(tmp_path: Path) -> Comms:
    root = tmp_path / "bus"
    return prepare_native_comms(Comms(root))


def test_every_bus_file_is_a_declared_root_file(tmp_path: Path, source_repository, comms: Comms) -> None:
    for name in ("owner", "peer"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    sent = comms.messaging.send("peer", "owner", "Declare the bus files").message
    resource = tmp_path / "owned.py"
    resource.write_text("owned\n")
    with NativeBoundary(comms.bus) as boundary:
        owner = boundary.registry.require("owner")
        claim = comms.bus.publisher._publish_acquisition_unlocked(
            boundary.registry, owner,
            ExistingFileClaim(resource).normalized(tmp_path),
            NativeClaim(
                owner_claimed_generation=boundary.registry.claim_generation(owner.name),
                operation_id="a" * 32, turn_id="declared-files", input_id="declared-file-input",
            ),
            activity=boundary.activity,
        )
    assert claim.seq > sent.seq
    todo = TodoStore(comms.root).create(
        "P1", RepositoryName.from_wire("OpenHCSDev/agent-comms"),
        "Keep declared bus files", creator=owner, assigner=owner.incarnation, owner=owner,
        generation="declared-files",
        request=comms.goals.todos.add_request(owner.incarnation, "Keep declared bus files").id,
        review_at=RecordedEventTime(1000.0), order=order(source_repository),
    )
    assert todo.id == "P1"
    declared = RootFile.paths_in(comms.root)
    assert PrivateOwnerRenameIntent.path_in(comms.root) in declared
    # Directory traversal observes the outcome; it is not the production inventory.
    actual = {path for path in comms.root.rglob("*") if path.is_file()}
    assert actual <= declared, sorted(str(path.relative_to(comms.root)) for path in actual - declared)
    durable = DurableFile.paths_in(comms.root)
    runtime = RuntimeFile.paths_in(comms.root)
    assert declared == durable | runtime
    assert not durable & runtime


@pytest.mark.parametrize("journal_mode", ("DELETE", "WAL"))
def test_parameterized_sqlite_resources_follow_their_file_owner(tmp_path: Path, journal_mode: str) -> None:
    proof = NativeInputProof.path_in(tmp_path, lookup="a" * 32, name="session.jsonl")
    proof.parent.mkdir(parents=True, mode=0o700)
    with sqlite3.connect(proof) as db:
        db.execute(f"PRAGMA journal_mode={journal_mode}")
        for statement in NativeContextJournal.schema_objects().values():
            db.execute(statement)
        db.commit()
        NativeContextJournal(
            input_id="b" * 32, session_id="session", session_entry_id="entry",
            request_generation=1, llm_context_digest="c" * 64,
            committed_at_ms=1_791_964_800_001, schema=2, type="context_committed",
        ).insert(db)
        actual = {path for path in tmp_path.rglob("*") if path.is_file()}
        assert len(actual) > 1  # The real SQLite writer has an open journal/WAL.
        assert actual <= NativeInputProof.files_in(tmp_path)
        assert str(proof.relative_to(tmp_path)) in bus_files(tmp_path)
