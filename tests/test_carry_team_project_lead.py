"""The real carry moves a stored team project to its new key and drops its lead."""

import json

from cutover.carry_team_project_lead import TeamProjectLead

from agent_comms.comms import wire
from agent_comms.locked_store import LiveFilesRequest, RegistryFile
from agent_comms.registry_document import RegisteredTeamProject
from conftest import maintenance_held


def test_carry_drops_the_stored_lead_and_the_project_stays_the_teams(tmp_path):
    comms = wire(tmp_path)
    comms.registry.set_team_project(RegisteredTeamProject(()))
    path = RegistryFile.path_in(tmp_path)
    document = json.loads(path.read_text())
    project = document.pop("team_project")
    document["commander_project"] = {**project, "commander": {"name": "lead", "created_at": 1.0}}
    path.write_text(json.dumps(document, indent=2))
    step = TeamProjectLead(LiveFilesRequest(tmp_path), tmp_path / "spill")

    assert step.carry(apply=False).changed == 1
    with maintenance_held(tmp_path):
        assert step.carry(apply=True).rewritten == (path.relative_to(tmp_path),)
    assert wire(tmp_path).registry.snapshot().team_project == RegisteredTeamProject(())
    assert step.carry(apply=False).changed == 0
