"""An install copies the registry before its carries run, so the copy's retirement cannot use the
current decoder: on 2026-11-04 the stored version 1 registry stopped every install at this step."""

from pathlib import Path

from agent_comms.registry.registry_store import RegistryStore
from agent_comms.storage.locked_store import RegistryFile

ALIVE = {"pid": 2828048, "start_time": 657855705}


def stored(root: Path) -> Path:
    """The keys the installed runtime wrote: a form this decoder refuses, with an extra field."""
    path = RegistryFile.path_in(root)
    RegistryStore(path).rewrite_stored(lambda _: {
        "version": 1, "commander_project": "retired", "threads": {
            "running": {"name": "running", "process_identity": ALIVE},
            "stopped": {"name": "stopped", "process_identity": None},
        }})
    return path


def test_a_copy_of_an_older_registry_names_no_live_process(tmp_path):
    source = stored(tmp_path)
    target = RegistryFile.path_in(tmp_path / "copy")
    target.parent.mkdir()
    before = RegistryStore(source).stored()
    RegistryFile.copy_group(source, target)
    copied = RegistryStore(target).stored()
    assert [row["process_identity"] for row in copied["threads"].values()] == [None, None]
    assert copied["version"] == 1 and copied["commander_project"] == "retired"
    assert copied["threads"]["running"]["name"] == "running"
    assert RegistryStore(source).stored() == before
