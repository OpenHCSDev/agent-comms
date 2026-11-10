"""The one-shot presence carry preserves timestamps before retiring its JSON source."""

import json

from cutover.carry_presence_rows import JsonPresenceFile, KeyedPresence

from agent_comms.registry.presence import PresenceStore
from agent_comms.storage.locked_store import LiveFilesRequest


def test_presence_carry_preserves_active_and_retired_names(tmp_path):
    root = tmp_path / "bus"
    root.mkdir(mode=0o700)
    source = JsonPresenceFile.path_in(root)
    seen = {"worker": 1234.5, "retired-worker": 1200.0}
    source.write_text(json.dumps(seen))
    step = KeyedPresence(LiveFilesRequest(root), tmp_path / "spill")
    assert step.carry(apply=False).changed == 1
    assert source.exists() and not PresenceStore(root).path.exists()
    changed = step.carry(apply=True)
    assert changed.changed == 1
    assert set(changed.rewritten) == {source.relative_to(root), PresenceStore(root).path.relative_to(root)}
    assert not source.exists()
    assert PresenceStore(root).read().seen == seen
    assert step.carry(apply=True).changed == 0
