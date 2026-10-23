"""Move the old commander parked-name list onto its registry threads once.

The historical home config is a link to the bus's commander.json. Read the
selected root's copy, never the home link, so rehearsals cannot reach the live
bus. Delete this step after every retained root has crossed the change.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import AtomicFile, _store_lock
from agent_comms.threads import ParkedThreadWorkState
from agent_comms.typed_table import HeldBusFile


class ParkedThreads(Carry):
    @property
    def configuration(self) -> Path:
        return self.root / "commander.json"

    def backup_files(self) -> tuple[Path, ...]:
        """Enumerate bytes to save without decoding the retired commander record."""
        paths = (self.configuration, *RegistryStore.at(self.root).written)
        for path in paths:
            self.require_confined(self.root, path)
        return tuple(path for path in paths if path.exists())

    def carry(self, *, apply: bool) -> Carried:
        self.require_confined(self.root, self.configuration)
        if not self.configuration.exists():
            return Carried(0)
        with _store_lock(self.configuration):
            raw = FieldCodec.decode(
                dict[str, object], FieldCodec.parse(self.configuration.read_text())
            )
            if "parked" not in raw:
                return Carried(0)
            names = FieldCodec.decode(tuple[str, ...], raw["parked"])
            registry = RegistryStore.at(self.root)
            parked = ParkedThreadWorkState()
            snapshot = registry.read_snapshot_unlocked()
            selected = tuple(dict.fromkeys(snapshot.require(name).name for name in names))
            changed = tuple(
                name for name in selected if snapshot.require(name).work_state != parked
            )
            if not apply:
                return Carried(len(changed) + 1, {"parked_threads": len(selected)})
            HeldBusFile(RegistryFile, registry.path).require()
            rewritten = []
            with registry.editing() as edit:
                changed = tuple(
                    name for name in selected if edit.document.require(name).work_state != parked
                )
                for name in changed:
                    edit.document.set_work_state(name, parked)
                edit.commit()
                if changed:
                    rewritten.extend(registry.written)
            del raw["parked"]
            AtomicFile(self.configuration).write_text(json.dumps(raw, indent=1) + "\n")
            rewritten.append(self.configuration)
            return Carried(
                len(changed) + 1,
                {"parked_threads": len(selected)},
                tuple(str(path.relative_to(self.root)) for path in rewritten),
            )
