"""Carry the current registry.json into rows once, then retire its JSON file.

The private guard keeps its prepare/commit protocol. This step verifies its commitment to the
source bytes before pointing it at the row counter. Delete the step after the live installation.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.execution.child_process import Platform
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.private_registry_guard import PrivateRegistryGuard, document_digest
from agent_comms.registry.registry_tables import RegistryCapture, RegistryRowStore
from agent_comms.storage.locked_store import RegistryFile, RootFile
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder


class RegistryRows(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile,)
    retired: ClassVar[str] = "registry.json"

    def input_files(self) -> tuple[Path, ...]:
        """Retain the source JSON, destination and guard before rehearsal or live conversion."""
        paths = {*super().input_files(), self.root / self.retired,
                 RegistryFile.path_in(self.root), PrivateRegistryGuard.path_in(self.root)}
        for path in paths:
            self.require_confined(path)
        return tuple(sorted(paths))

    def carry(self, *, apply: bool) -> Carried:
        source = self.root / self.retired
        self.require_confined(source)
        store = RegistryRowStore(self.root)
        self.require_confined(store.path)
        if not source.exists():
            return Carried(0)
        if not apply:
            return Carried(1)
        MaintenanceBarrier(store.path).read().require_held_by(
            MaintenanceHolder.current(), "Carry the registry's rows into registry.sqlite3")
        content = source.read_bytes()
        raw = FieldCodec.decode(dict[str, JsonValue], FieldCodec.parse(content))
        captured = store.captured(RegistryCapture.empty())
        mark = captured.mark
        if not mark.revision:
            mark = store.commit(raw, captured)
        elif captured.raw != raw:
            raise ValueError("Registry rows differ from the JSON this interrupted carry must preserve")
        written = [store.path, source]
        guard = PrivateRegistryGuard.path_in(self.root)
        self.require_confined(guard)
        if guard.exists():
            PrivateRegistryGuard.recommit_carried(store.path, document_digest(content), mark)
            written.append(guard)
        source.unlink()
        Platform.current().sync_directory(self.root)
        return Carried(1, rewritten=tuple(path.relative_to(self.root) for path in written))
