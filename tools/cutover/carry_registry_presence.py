"""Move each thread's ``last_seen`` out of registry.json into presence.json.

Version 2 stored presence inside the registry, so every heartbeat rewrote the whole document
(5.7 MB on the live bus) for one timestamp. Version 3 keeps it in PresenceStore and the decoder
refuses version 2, so this step writes the stored presence first, then stamps the registry
version 3 without it, the order a registry edit uses. Delete this step after the live
installation.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.presence import PresenceDocument, PresenceStore
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.storage.locked_store import DocumentChange, RegistryFile, RootFile
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder


class RegistryPresence(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile, PresenceStore)
    retired_key: ClassVar[str] = "last_seen"

    def carry(self, *, apply: bool) -> Carried:
        path = RegistryFile.path_in(self.root)
        self.require_confined(path)
        if not path.exists():
            return Carried(0)
        document = FieldCodec.decode(
            dict[str, JsonValue], FieldCodec.parse(path.read_text(encoding="utf-8"))
        )
        version = FieldCodec.record_key(RegistryDocument, "version")
        if document[version] == RegistryDocument.version:
            return Carried(0)
        seen = FieldCodec.decode(dict[str, float], document.pop(self.retired_key))
        document[version] = RegistryDocument.version
        registry, presence = RegistryStore(path), PresenceStore.at(self.root)
        self.require_confined(presence.path)
        if apply:
            MaintenanceBarrier(path).read().require_held_by(
                MaintenanceHolder.current(), "Move thread presence out of the registry"
            )
            presence.update(
                lambda stored: DocumentChange.to(PresenceDocument({**stored.seen, **seen})))
            registry.carry_text(json.dumps(document, indent=2))
        return Carried(
            1,
            rewritten=(
                tuple(written.relative_to(self.root)
                      for written in (presence.path, *registry.written))
                if apply
                else ()
            ),
        )
