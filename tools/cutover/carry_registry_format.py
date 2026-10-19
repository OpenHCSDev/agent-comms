"""P669: convert the registry before any target-format reader opens it."""
from pathlib import Path

from agent_comms.registry_store import RegistryStore

from .carry import Carried, Carry


class RegistryFormat(Carry):
    after = None

    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        store = RegistryStore(root / 'registry.json')
        return store.path, store.guard_path

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        store = RegistryStore(root / 'registry.json')
        changed = store.convert_format(apply=apply)
        rewritten = tuple(path.name for path in cls.backup_files(root)) if changed and apply else ()
        return Carried(int(changed), rewritten=rewritten)


if __name__ == '__main__':
    RegistryFormat.main()
