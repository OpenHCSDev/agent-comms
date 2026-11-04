"""The real carry stamps every registry version 2 and drops a stored project's lead."""

import json

import pytest
from cutover.carry_team_project_lead import TeamProjectLead

from agent_comms.comms import wire
from agent_comms.locked_store import LiveFilesRequest, RegistryFile
from agent_comms.registry_document import NoTeamProject, RegisteredTeamProject
from agent_comms.registry_store import RegistryStore
from agent_comms.repo import Checkout
from agent_comms.threads import Thread
from conftest import maintenance_held


def version_one(root, *, lead: bool) -> None:
    """Rewrite a fresh registry in the version-1 form an installed runtime left, through its
    private guard as that runtime wrote it, so the guard records those bytes."""
    path = RegistryFile.path_in(root)
    document = json.loads(path.read_text())
    document["version"] = 1
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
    comms.registry.declare(Thread("worker", frozenset(), Checkout(tmp_path)))  # a stored registry
    comms.registry.set_team_project(project)
    version_one(tmp_path, lead=lead)
    path = RegistryFile.path_in(tmp_path)
    step = TeamProjectLead(LiveFilesRequest(tmp_path), tmp_path / "spill")

    assert step.carry(apply=False).changed == 1
    with maintenance_held(tmp_path):
        assert step.carry(apply=True).rewritten == (path.relative_to(tmp_path),)
    assert wire(tmp_path).registry.snapshot().team_project == project
    assert step.carry(apply=False).changed == 0
