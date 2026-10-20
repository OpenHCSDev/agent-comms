"""Discard disposable runtime observations after removing the unused session name."""
from pathlib import Path

from agent_comms.runtime_info import RuntimeInfoStore
from agent_comms.installer.carry import Carried, Carry


class RuntimeInfo(Carry):

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return (RuntimeInfoStore.at(root).path,)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = RuntimeInfoStore.at(root).path
        if not path.exists():
            return Carried(0)
        if apply:
            path.unlink()
        return Carried(1, rewritten=(path.name,))
