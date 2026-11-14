"""Move presence rows into the registry's own file.

Presence had a SQLite file of its own, ``presence.sqlite3``, because ``registry.json`` was
rewritten whole on every write and 19 of 39 sampled writes changed only a last-seen time
(9c46426cc). The registry's rows have been in ``registry.sqlite3`` since 866de382d, one row per
commit, so presence is two tables beside them now: an edit commits its rows and its presence
in one transaction, and a heartbeat still moves only the presence header.

The carry creates the presence family's two tables beside the registry's from their
declaration (PresenceSchema) and copies every presence row and the presence header unchanged, in one transaction per registry.
Once the install is accepted it removes the old file, its WAL, its lock, and the lock
``presence.json`` left behind when it became ``presence.sqlite3``.
"""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import ClassVar

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.registry.registry_tables import (
    PresenceHeader,
    PresenceRow,
    PresenceSchema,
    RegistryRowStore,
)
from agent_comms.storage.locked_store import RegistryFile, RootFile


class PresenceIntoRegistry(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile,)
    previous: ClassVar[str] = "presence.sqlite3"
    beside: ClassVar[tuple[str, ...]] = (
        "{name}-wal", "{name}-shm", ".{name}.lock", ".presence.json.lock")
    """The old file's WAL and lock, and the lock of the JSON file it replaced."""

    def stored(self, registry: Path) -> Path:
        """The old presence file beside one registry."""
        return registry.with_name(self.previous)

    def input_files(self) -> tuple[Path, ...]:
        """The old file is an input like the registry: the practice copy holds it, so the
        rehearsal runs this carry, and a failed install puts it back."""
        registries = super().input_files()
        retired = tuple(path for registry in registries
                        if (path := self.stored(registry)).is_file())
        for path in retired:
            self.require_confined(path)
        return tuple(sorted({*registries, *retired}))

    def presence(self, registry: Path) -> tuple[list[tuple[str, float, int]], int] | None:
        path = self.stored(registry)
        if not path.is_file():
            return None
        with closing(sqlite3.connect(path)) as db:
            rows = db.execute('SELECT "name", "seen", "changed_at" FROM "presence"').fetchall()
            header = db.execute(
                'SELECT "revision" FROM "presence_header" WHERE "header" = 1').fetchone()
        return rows, header[0] if header else 0

    def carry(self, *, apply: bool) -> Carried:
        carried = {registry: found for registry in super().input_files()
                   if (found := self.presence(registry)) is not None}
        if apply:
            for registry, (rows, revision) in carried.items():
                with (closing(RegistryRowStore.writer(registry)) as db,
                      RegistryRowStore.write_transaction(db)):
                    if not PresenceSchema.stored_schema(db):
                        PresenceSchema.create_schema(db)
                    for name, seen, changed_at in rows:
                        PresenceRow(changed_at=changed_at, name=name, seen=seen).upsert(db)
                    if revision:
                        PresenceHeader(header=1, revision=revision).upsert(db)
        return Carried(
            len(carried),
            detail={"registries": len(carried),
                    "rows": sum(len(rows) for rows, _ in carried.values())},
            rewritten=tuple(path.relative_to(self.root) for path in carried) if apply else (),
        )

    def after_acceptance(self) -> Carried:
        """A restore needs the old file to put presence back, so it outlives the rollback
        window. Its WAL, lock and the JSON file's lock are not content and go with it."""
        retired = tuple(path for registry in super().input_files()
                        if (path := self.stored(registry)).is_file())
        for path in retired:
            path.unlink()
            for name in self.beside:
                path.with_name(name.format(name=path.name)).unlink(missing_ok=True)
        return Carried(len(retired))
