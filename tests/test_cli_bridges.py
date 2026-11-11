"""Bridge capabilities share history declarations without adding standalone readers."""

from pathlib import Path

from agent_comms.cli_bridges.history import HarnessHistory, History
from agent_comms.cli_bridges.shared import BridgeDeliveryStore, CliBridge
from agent_comms.installer.harness_install import HarnessSession


def test_bridges_list_their_own_state_directories(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    bridges = HarnessSession.members_with(CliBridge)
    assert bridges
    assert BridgeDeliveryStore.path() == tmp_path
    for bridge in bridges:
        assert bridge.state_path().is_relative_to(tmp_path)
    for parent in HarnessHistory.__subclasses__():
        assert not set(History.readers(parent.harness)).intersection(bridges)
