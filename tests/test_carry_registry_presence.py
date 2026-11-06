"""The real carry moves a version 2 registry's stored presence into presence.json."""

import json

from cutover.carry_registry_presence import RegistryPresence

from agent_comms.comms import wire
from agent_comms.presence import PresenceStore
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.repo import Checkout
from agent_comms.storage.locked_store import LiveFilesRequest, RegistryFile
from agent_comms.registry.threads import NativeThread
from conftest import maintenance_held


def test_carry_moves_stored_presence_out_of_the_registry(private_directory):
    tmp_path = private_directory
    comms = wire(tmp_path)
    comms.registry.store.prepare_native_root()
    comms.registry.declare(NativeThread("worker", frozenset(), Checkout(tmp_path)))
    path, presence = RegistryFile.path_in(tmp_path), PresenceStore.at(tmp_path)
    # Rewrite it in the version 2 form an installed runtime left: presence inline, no presence.json.
    document = json.loads(path.read_text())
    document["version"], document["last_seen"] = 2, {"worker": 1234.5}
    RegistryStore(path).carry_text(json.dumps(document, indent=2))
    presence.path.unlink()
    step = RegistryPresence(LiveFilesRequest(tmp_path), tmp_path / "spill")

    assert step.carry(apply=False).changed == 1 and not presence.path.exists()
    with maintenance_held(tmp_path):
        assert step.carry(apply=True).rewritten == tuple(
            written.relative_to(tmp_path)
            for written in (presence.path, path, PrivateRegistryGuard.path_in(tmp_path)))
    stored = json.loads(path.read_text())
    assert stored["version"] == 3 and "last_seen" not in stored
    assert wire(tmp_path).registry.last_seen("worker") == 1234.5
    assert step.carry(apply=False).changed == 0
