"""Move each leased turn's file into the registry's own file.

A leased turn was a JSON file of its own, ``turns/{lookup}-{generation}.json`` beside the
registry, so a phase change took the turn's lock and never the registry's (35165284f). A lease
then wrote the turn file before the registry commit that named it and deleted the superseded
file after it: one edit, three writes, and a window where a turn existed that no registry
revision named. The turn is now a row of the registry's file in its own table family: the lease
writes it in the commit that names it, a phase change rewrites it in a SQLite transaction of its
own, and the superseding lease deletes it in its commit.

This copies every turn file's record unchanged, decoded once to refuse a damaged one, in one
transaction per registry. Once the install is accepted it removes the files and their locks.
"""

import json
from contextlib import closing
from pathlib import Path
from typing import ClassVar

from agent_comms.execution.turn_lease import ActiveTurn
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec, ThreadLookup
from agent_comms.registry.registry_tables import (
    RegistryRowStore,
    StoredWire,
    TurnRow,
    TurnSchema,
)
from agent_comms.storage.locked_store import RegistryFile, RootFile


class TurnsIntoRegistry(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile,)
    directory: ClassVar[str] = "turns"
    """Where the turn files lived beside each registry; nothing else reads this layout."""

    def stored(self, registry: Path) -> tuple[Path, ...]:
        """Each turn file beside one registry, named ``{lookup}-{generation}.json``."""
        return tuple(sorted((registry.parent / self.directory).glob("*.json")))

    def input_files(self) -> tuple[Path, ...]:
        """The turn files are inputs like the registry: the practice copy holds them, so the
        rehearsal runs this carry, and a failed install puts them back."""
        registries = super().input_files()
        retired = tuple(path for registry in registries for path in self.stored(registry))
        for path in retired:
            self.require_confined(path)
        return tuple(sorted({*registries, *retired}))

    @staticmethod
    def row(path: Path) -> TurnRow:
        lookup, generation = path.stem.rsplit("-", 1)
        wire = json.loads(path.read_text())
        FieldCodec.decode_document(ActiveTurn, wire)
        return TurnRow(lookup=ThreadLookup(lookup), generation=int(generation),
                       active=StoredWire(wire))

    def carry(self, *, apply: bool) -> Carried:
        carried = {registry: tuple(self.row(path) for path in self.stored(registry))
                   for registry in super().input_files()}
        carried = {registry: rows for registry, rows in carried.items() if rows}
        if apply:
            for registry, rows in carried.items():
                with (closing(RegistryRowStore.writer(registry)) as db,
                      RegistryRowStore.write_transaction(db)):
                    if not TurnSchema.stored_schema(db):
                        TurnSchema.create_schema(db)
                    for row in rows:
                        row.upsert(db)
        return Carried(
            len(carried),
            detail={"registries": len(carried),
                    "turns": sum(len(rows) for rows in carried.values())},
            rewritten=tuple(path.relative_to(self.root) for path in carried) if apply else (),
        )

    def after_acceptance(self) -> Carried:
        """A restore needs the files to put the turns back, so they outlive the rollback
        window. Each file's lock is not content and goes with it."""
        retired = tuple(path for registry in super().input_files() for path in self.stored(registry))
        for path in retired:
            path.unlink()
            path.with_name(f".{path.name}.lock").unlink(missing_ok=True)
        for registry in super().input_files():
            directory = registry.parent / self.directory
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        return Carried(len(retired))
