"""The real carry stamps every registry version 2 and drops a stored project's lead."""

import json

import pytest
from cutover.carry_registry_presence import RegistryPresence
from cutover.carry_team_project_lead import TeamProjectLead

from agent_comms.comms import wire
from agent_comms.presence import PresenceStore
from agent_comms.storage.locked_store import LiveFilesRequest, RegistryFile
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.registry.registry_document import NoTeamProject, RegisteredTeamProject
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.repo import Checkout
from agent_comms.registry.threads import NativeThread
from conftest import maintenance_held


def version_one(root, *, lead: bool) -> None:
    """Rewrite a fresh registry in the version-1 form an installed runtime left, through its
    private guard as that runtime wrote it, so the guard records those bytes."""
    path = RegistryFile.path_in(root)
    document = json.loads(path.read_text())
    document["version"] = 1
    # Versions 1 and 2 stored each thread's presence inline.
    document["last_seen"] = PresenceStore.at(root).read().seen
    for row in document["threads"].values():  # version 1 omitted the native default
        if row.pop("execution") != "native":
            raise AssertionError("the fixture declares only native threads")
    project = document.pop("team_project", None)
    if lead:
        document["commander_project"] = {**project, "commander": {"name": "lead", "created_at": 1.0}}
    RegistryStore(path).carry_text(json.dumps(document, indent=2))


@pytest.mark.parametrize(("project", "lead"), [
    (RegisteredTeamProject(()), True),
    (NoTeamProject(), False),
], ids=["team-with-lead", "no-team"])
def test_carry_stamps_version_two_and_the_project_stays_the_teams(private_directory, project, lead):
    # A live root: its registry has the private guard that records the registry's own bytes.
    tmp_path = private_directory
    comms = wire(tmp_path)
    comms.registry.store.prepare_native_root()
    comms.registry.declare(NativeThread("worker", frozenset(), Checkout(tmp_path)))  # a stored registry
    comms.registry.set_team_project(project)
    version_one(tmp_path, lead=lead)
    path = RegistryFile.path_in(tmp_path)
    step = TeamProjectLead(LiveFilesRequest(tmp_path), tmp_path / "spill")

    assert step.carry(apply=False).changed == 1
    with maintenance_held(tmp_path):
        # The guard is rewritten with the registry, so the upgrade's file audit expects both.
        assert step.carry(apply=True).rewritten == tuple(
            written.relative_to(tmp_path)
            for written in (path, PrivateRegistryGuard.path_in(tmp_path)))
    with maintenance_held(tmp_path):
        assert RegistryPresence(LiveFilesRequest(tmp_path), tmp_path / "spill").carry(apply=True).changed == 1
    assert wire(tmp_path).registry.snapshot().team_project == project
    assert type(wire(tmp_path).registry.require("worker")) is NativeThread
    assert step.carry(apply=False).changed == 0
