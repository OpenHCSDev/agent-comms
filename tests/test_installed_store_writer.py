"""The published route, not a caller's override, decides which root is live."""
import json
import sys
from pathlib import Path

import pytest

from agent_comms.active_route import ActiveRoute, active_route_path
from agent_comms.errors import RelationViolationError
from agent_comms.owner_cutover import StoppedOwnerInstallation
from agent_comms.runtime_info import RuntimeInfoDocument, RuntimeInfoStore


def test_branch_source_cannot_write_live_root_with_matching_python(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    store = RuntimeInfoStore.at(root)
    store.replace(RuntimeInfoDocument())
    before = store.path.read_bytes()

    pointer = StoppedOwnerInstallation.current_pointer()
    pointer.parent.mkdir(parents=True, mode=0o700)
    pointer.symlink_to(Path(sys.prefix).resolve())
    # This source test uses the selected Python but Core from the checkout.
    import agent_comms.owner_cutover as installation
    assert not Path(installation.__file__).resolve().is_relative_to(pointer.resolve())
    route = ActiveRoute(root, "1" * 32)
    path = active_route_path()
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(json.dumps(route.to_record()))
    path.chmod(0o600)
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path / "different-root"))

    with pytest.raises(RelationViolationError, match="installed current Core runtime"):
        store.replace(RuntimeInfoDocument())
    assert store.path.read_bytes() == before
