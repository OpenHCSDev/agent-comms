"""An install copies the registry before its carries run, so the copy's retirement cannot use the
current decoder: on 2026-11-04 the stored version 1 registry stopped every install at this step."""

import json
from pathlib import Path

from agent_comms.storage.locked_store import RegistryFile
from agent_comms.registry_store import RegistryStore

ALIVE = {"pid": 2828048, "start_time": 657855705}


def stored(root: Path) -> Path:
    """The keys the installed runtime wrote: a form this decoder refuses, with an extra field."""
    path = root / "registry.json"
    RegistryStore(path).carry_text(json.dumps({
        "version": 1, "commander_project": "retired", "threads": {
            "running": {"name": "running", "process_identity": ALIVE},
            "stopped": {"name": "stopped", "process_identity": None},
        }}))
    return path


def test_a_copy_of_an_older_registry_names_no_live_process(tmp_path):
    source = stored(tmp_path)
    target = tmp_path / "copy" / "registry.json"
    target.parent.mkdir()
    before = source.read_text()
    RegistryFile.copy_group(source, target)
    copied = json.loads(target.read_text())
    assert [row["process_identity"] for row in copied["threads"].values()] == [None, None]
    assert copied["version"] == 1 and copied["commander_project"] == "retired"
    assert copied["threads"]["running"]["name"] == "running"
    assert source.read_text() == before
