"""Bridge capabilities share history declarations without adding standalone readers."""

from pathlib import Path

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.codex import CodexBridge
from agent_comms.cli_bridges.history import HarnessHistory, History
from agent_comms.harness_install import HarnessSession
from agent_comms.cli_bridges.opencode import OpenCodeBridge
from agent_comms.cli_bridges.shared import BridgeDeliveryStore, CliBridge


def test_bridges_list_their_own_state_directories(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    bridges = HarnessSession.members_with(CliBridge)
    assert set(bridges) == {ClaudeBridge, CodexBridge, OpenCodeBridge}
    assert BridgeDeliveryStore.path() == tmp_path
    for bridge in bridges:
        assert bridge.state_path() == tmp_path / bridge.state_directory
    for parent in HarnessHistory.__subclasses__():
        assert not set(History.readers(parent.harness)).intersection(bridges)
