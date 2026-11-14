"""Record each private registry's native namespace as a row of the registry's own file.

The namespace lives in the guard file's two slots (``.registry-owner-guard``), written to
bracket every registry commit since the registry was a JSON file whose atomic replacement
could be visible before its directory sync. The registry has committed rows through SQLite
since 866de382d, so the namespace becomes a row of its own family, and the next install
moves readers to it and deletes the bracket. Readers stay on the guard here: the target
runtime reads a root's privacy before any carry runs, so the row must exist one install
ahead of its readers.

This copies each guard's namespace, verified against its slots and the registry revision
they name, into the registry's namespace row. The guard file stays.
"""

from contextlib import closing
from pathlib import Path
from typing import ClassVar

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.registry.private_registry_guard import PrivateRegistryGuard
from agent_comms.registry.registry_tables import (
    NamespaceSchema,
    RegistryNamespace,
    RegistryRowStore,
)
from agent_comms.storage.locked_store import RegistryFile, RootFile


class RegistryNamespaceRow(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile,)

    def guarded(self) -> tuple[Path, ...]:
        """The registries that have a guard: the private roots."""
        return tuple(registry for registry in super().input_files()
                     if PrivateRegistryGuard.path_in(registry.parent).is_file())

    def input_files(self) -> tuple[Path, ...]:
        registries = super().input_files()
        guards = tuple(PrivateRegistryGuard.path_in(registry.parent) for registry in self.guarded())
        for path in guards:
            self.require_confined(path)
        return tuple(sorted({*registries, *guards}))

    def carry(self, *, apply: bool) -> Carried:
        namespaces = {registry: PrivateRegistryGuard.load(registry).namespace
                      for registry in self.guarded()}
        if apply:
            for registry, root_id in namespaces.items():
                with (closing(RegistryRowStore.writer(registry)) as db,
                      RegistryRowStore.write_transaction(db)):
                    if not NamespaceSchema.stored_schema(db):
                        NamespaceSchema.create_schema(db)
                    RegistryNamespace(header=1, root_id=root_id).insert_if_absent(db)
        return Carried(
            len(namespaces),
            detail={"registries": len(namespaces)},
            rewritten=tuple(path.relative_to(self.root) for path in namespaces) if apply else (),
        )
