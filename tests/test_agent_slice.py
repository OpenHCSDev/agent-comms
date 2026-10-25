"""The systemd boundary uses the declared CPU slice without contacting a manager."""

from agent_comms.child_process import NoResidentManager, SystemdUserManager
from agent_comms.host_services.connector import SystemdConnectorService
from agent_comms.systemd_units import AgentsSlice


def test_resident_command_uses_the_agent_slice():
    command = ("python", "-m", "agent_comms.worker")
    scoped = SystemdUserManager.command(command)
    assert "--slice=agents.slice" in scoped
    assert scoped[-len(command):] == command
    assert NoResidentManager.command(command) == command


def test_slice_definition_and_service_share_the_declared_resources(tmp_path):
    AgentsSlice.write_unit(home=tmp_path)
    rendered = AgentsSlice.unit_file(tmp_path).read_text()
    assert "[Slice]\nAllowedCPUs=2-11\nCPUWeight=50\n" in rendered
    assert f"Slice={AgentsSlice.unit}\n" in SystemdConnectorService.unit_text(())
