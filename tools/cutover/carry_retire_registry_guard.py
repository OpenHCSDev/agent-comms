"""Retire the registry guard file now that the namespace is a row of the registry's file.

``.registry-owner-guard`` held a private root's native namespace in two slots and bracketed
every registry commit with a pending and a committed write, each fsynced and each waking every
wire watcher. The bracket answered a JSON registry whose replacement could be visible before
its directory sync; the registry has committed SQLite rows since 866de382d, and a crash between
the row commit and the committed slot left a root that refused every read. The namespace is a
row since #4765 and the reader of privacy is that row.

For each guard file this records the namespace from the guard's header in the registry's
namespace row if the row is absent, refuses a row that names a different namespace, and once
the install is accepted removes the file. A restore puts the file back with the backup.
"""

import struct
from contextlib import closing
from pathlib import Path
from typing import ClassVar

from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import RootId
from agent_comms.registry.registry_tables import (
    NamespaceSchema,
    RegistryNamespace,
    RegistryRowStore,
)
from agent_comms.storage.locked_store import RegistryFile, RootFile


class RetireRegistryGuard(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile,)
    filename: ClassVar[str] = ".registry-owner-guard"
    header: ClassVar[struct.Struct] = struct.Struct("!4sB16s")
    """The retired file's leading magic, version and namespace: the only part read here."""

    def guard(self, registry: Path) -> Path:
        return registry.with_name(self.filename)

    def guarded(self) -> tuple[Path, ...]:
        return tuple(registry for registry in super().input_files()
                     if self.guard(registry).is_file())

    def input_files(self) -> tuple[Path, ...]:
        registries = super().input_files()
        guards = tuple(self.guard(registry) for registry in self.guarded())
        for path in guards:
            self.require_confined(path)
        return tuple(sorted({*registries, *guards}))

    def namespace(self, registry: Path) -> RootId:
        magic, _, root = self.header.unpack(self.guard(registry).read_bytes()[: self.header.size])
        if magic != b"ACRG":
            raise RelationViolationError(f"Not a registry guard: {self.guard(registry)}")
        return RootId(root.hex())

    def carry(self, *, apply: bool) -> Carried:
        namespaces = {registry: self.namespace(registry) for registry in self.guarded()}
        if apply:
            for registry, root_id in namespaces.items():
                with (closing(RegistryRowStore.writer(registry)) as db,
                      RegistryRowStore.write_transaction(db)):
                    if not NamespaceSchema.stored_schema(db):
                        NamespaceSchema.create_schema(db)
                    if any(row.root_id != root_id for row in RegistryNamespace.select(db)):
                        raise RelationViolationError(
                            f"{registry} records another namespace than its guard")
                    RegistryNamespace(header=1, root_id=root_id).insert_if_absent(db)
        return Carried(
            len(namespaces),
            detail={"registries": len(namespaces)},
            rewritten=tuple(path.relative_to(self.root) for path in namespaces) if apply else (),
        )

    def after_acceptance(self) -> Carried:
        """A restore needs the guard to put the old protocol back, so it outlives the rollback
        window."""
        retired = tuple(self.guard(registry) for registry in self.guarded())
        for path in retired:
            path.unlink()
        return Carried(len(retired))
