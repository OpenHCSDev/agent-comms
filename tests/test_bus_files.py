"""The real bus publishes only files declared by their existing state owners."""

from pathlib import Path

from agent_comms.candidate_maintenance import drain_candidate_catchup
from agent_comms.comms import Comms
from agent_comms.coordination_session import NativeBoundary
from agent_comms.envelope_claim_transitions import ExistingFileClaim, NativeClaim
from agent_comms.locked_store import DurableFile, RootFile, RuntimeFile
from agent_comms.private_registry_guard import PrivateOwnerRenameIntent
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import GoalRef, TodoStore
from test_todos import order


def test_every_bus_file_is_a_declared_root_file(tmp_path: Path) -> None:
    comms = Comms(tmp_path / "bus")
    comms.messaging.initialize_private_initial_protocol()
    for name in ("owner", "peer"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    sent = comms.messaging.send("peer", "owner", "Declare the bus files").message
    resource = tmp_path / "owned.py"
    resource.write_text("owned\n")
    with NativeBoundary(comms.bus) as boundary:
        owner = boundary.registry.require("owner")
        claim = comms.bus.publisher._publish_acquisition_unlocked(
            boundary.bus_lock, boundary.registry, owner,
            ExistingFileClaim(resource).normalized(tmp_path),
            NativeClaim(
                owner_claimed_generation=boundary.registry.claimed_generations[owner.name],
                operation_id="a" * 32, turn_id="declared-files", input_id="declared-file-input",
            ),
            activity=boundary.activity,
        )
    assert claim.seq > sent.seq
    todo = TodoStore(comms.root).create(
        "declared-files", RepositoryName.from_wire("OpenHCSDev/agent-comms"),
        "Keep declared bus files", creator=owner, owner=owner,
        generation="declared-files", goal=GoalRef(owner.name, owner.created_at, "goal-1"),
        deadline=RecordedEventTime(1000.0), order=order(comms.root),
    )
    assert todo.id == "declared-files"
    drain_candidate_catchup(comms.root)
    declared = RootFile.paths_in(comms.root)
    assert PrivateOwnerRenameIntent.path_in(comms.root) in declared
    # Directory traversal observes the outcome; it is not the production inventory.
    actual = {path for path in comms.root.rglob("*") if path.is_file()}
    assert actual <= declared, sorted(str(path.relative_to(comms.root)) for path in actual - declared)
    durable = DurableFile.paths_in(comms.root)
    runtime = RuntimeFile.paths_in(comms.root)
    assert declared == durable | runtime
    assert not durable & runtime
