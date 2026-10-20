"""Convert the registry to the version its store declares (P669).

The store owns the conversion and validates the whole converted document before
its guarded writer publishes it; this step only runs it while the installer
holds every old reader stopped.
"""
from pathlib import Path

from agent_comms.registry_store import RegistryStore
from agent_comms.installer.carry import Carried, Carry


class RegistryVersion(Carry):

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return RegistryStore.at(root).written

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        store = RegistryStore.at(root)
        if not store.convert_format(apply=apply):
            return Carried(0)
        return Carried(1, rewritten=tuple(path.name for path in store.written))
