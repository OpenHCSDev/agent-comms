"""Carry registry.json into registry.sqlite3's rows, re-point its guard, and retire the file.

The whole-document registry rewrote every row on every change (5.6 MB and 2,933 threads on the
live bus to change one); RegistryRowStore writes the rows a change changes. This step commits the
stored document once as revision 1, moves a private root's guard from registry.json's byte digest
to that revision (refusing a guard that vouched for other bytes), then deletes registry.json. Each
part is idempotent, so a step interrupted between them finishes on its next run. Delete this step
after the live installation.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import JsonValue

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.private_registry_guard import PrivateRegistryGuard, document_digest
from agent_comms.registry.registry_tables import RegistryCapture, RegistryRowStore
from agent_comms.storage.locked_store import RegistryFile, RootFile
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder


class RegistryRows(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile,)
    retired: ClassVar[str] = "registry.json"

    def carry(self, *, apply: bool) -> Carried:
        source = self.root / self.retired
        self.require_confined(source)
        store = RegistryRowStore(self.root)
        self.require_confined(store.path)
        if not source.exists():
            return Carried(0)
        if apply:
            MaintenanceBarrier(store.path).read().require_held_by(
                MaintenanceHolder.current(), "Carry the registry's rows into registry.sqlite3")
            content = source.read_bytes()
            mark = store.mark()
            if not mark.revision:
                raw = FieldCodec.decode(dict[str, JsonValue], FieldCodec.parse(content))
                mark = store.commit(raw, RegistryCapture.empty())
            if PrivateRegistryGuard.path_in(self.root).exists():
                PrivateRegistryGuard.recommit_carried(store.path, document_digest(content), mark)
            source.unlink()
        return Carried(
            1, rewritten=(store.path.relative_to(self.root), source.relative_to(self.root))
            if apply else ())
