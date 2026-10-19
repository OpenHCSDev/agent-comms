"""Carry registry version 1 before publishing connected compaction presence."""

import json
from dataclasses import fields
from pathlib import Path

from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore

from .carry import Carried, Carry


class CompactingStatus(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        store = RegistryStore(root / "registry.json")
        return store.path, store.guard_path

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        store = RegistryStore(root / "registry.json")
        if not store.path.exists():
            return Carried(0)
        version = fields(RegistryDocument)[0]
        with store.locked():
            raw = json.loads(store.path.read_text())
            previous = raw[version.name]
            if previous == version.default:
                return Carried(0)
            if previous != 1:
                raise ValueError(f"Unexpected previous registry version: {previous}")
            raw[version.name] = version.default
            document = RegistryDocument.from_wire(raw)
            if apply:
                store.save_unlocked(document)
        return Carried(1)


if __name__ == "__main__":
    CompactingStatus.main()
