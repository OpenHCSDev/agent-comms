"""Old home-only commander facts move through the config's own writer."""
import shutil

from agent_comms.commander.config import CommanderConfig
from cutover.carry_commander_config import CommanderConfigRoot


def test_home_record_is_carried_once_on_a_copied_root(commander, tmp_path, monkeypatch):
    comms, _ = commander
    original = CommanderConfig.read_root(comms.root)
    before = CommanderConfig.root_path(comms.root).read_bytes()
    root = tmp_path / "copied-root"
    shutil.copytree(comms.root, root)
    CommanderConfig.root_path(root).unlink()
    home = tmp_path / "copied-home"
    old = CommanderConfig.path(home)
    old.parent.mkdir(parents=True)
    old.write_text(original.model_dump_json())
    monkeypatch.setenv("HOME", str(home))
    assert CommanderConfigRoot.carry(root, apply=False).changed == 1
    assert not old.is_symlink() and not CommanderConfig.root_path(root).exists()
    assert CommanderConfigRoot.carry(root, apply=True).changed == 1
    assert old.is_symlink() and old.resolve() == CommanderConfig.root_path(root)
    assert CommanderConfig.read_root(root) == original.model_copy(update={"root": root})
    assert CommanderConfigRoot.carry(root, apply=True).changed == 0
    assert CommanderConfig.root_path(comms.root).read_bytes() == before
