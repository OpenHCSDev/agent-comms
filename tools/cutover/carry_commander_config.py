"""Move the old home-only commander record through its existing writer."""
from __future__ import annotations

from pathlib import Path

from agent_comms.commander.config import CommanderConfig
from .carry import Carried, Carry


class CommanderConfigRoot(Carry):
    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        home = Path.home()
        old = CommanderConfig.path(home)
        record = CommanderConfig.root_path(root)
        if record.exists() or old.is_symlink() or not old.is_file():
            return Carried(0)
        # A cutover rehearsal selects its copied root, never the live root named
        # in the old machine file. The selected root owns the new record.
        config = CommanderConfig.read(old).model_copy(update={"root": root})
        if apply:
            config.write(home)
        return Carried(1, {"commander_configs": 1}, (record.name,) if apply else ())


if __name__ == "__main__":
    CommanderConfigRoot.main()
