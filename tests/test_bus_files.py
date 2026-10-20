"""The real bus publishes only files declared by their existing state owners."""

from pathlib import Path

from agent_comms.locked_store import DurableFile, RootFile, RuntimeFile
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.repository_name import RepositoryName
from agent_comms.todos import GoalRef, TodoStore

from test_checkpoint_resource_claims import authored, authorized
from test_todos import order


def test_every_bus_file_is_a_declared_root_file(tmp_path: Path) -> None:
    # These helpers use real Comms, registration, message publication, wire locks
    # and claim/checkpoint writes. No fake store, publisher or filesystem.
    comms, resources, scope = authored(tmp_path)
    claimed = authorized(comms, scope, resources[:1])
    assert claimed.seq > 0
    owner = comms.registry.require("owner")
    todo = TodoStore(comms.root).create(
        "declared-files", RepositoryName.from_wire("OpenHCSDev/agent-comms"),
        "Keep declared bus files", creator=owner, owner=owner,
        generation="declared-files", goal=GoalRef(owner.name, owner.created_at, "goal-1"),
        deadline=RecordedEventTime(1000.0), order=order(comms.root),
    )
    assert todo.id == "declared-files"
    declared = RootFile.paths_in(comms.root)
    # Directory traversal observes the outcome; it is not the production inventory.
    actual = {path for path in comms.root.rglob("*") if path.is_file()}
    assert actual <= declared, sorted(str(path.relative_to(comms.root)) for path in actual - declared)
    durable = DurableFile.paths_in(comms.root)
    runtime = RuntimeFile.paths_in(comms.root)
    assert declared == durable | runtime
    assert not durable & runtime
