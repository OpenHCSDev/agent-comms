"""Drop the stored lead from the registry's team project, which now belongs to the team.

The registry kept the project under ``commander_project`` with a ``commander`` incarnation:
one thread recorded as the team's lead. The project is now ``team_project`` with only its
repositories, and the version-2 decoder refuses version 1, so this step stamps every
registry version 2, moving a stored project to its new key without its lead. Delete this
step after the live installation.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile, RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore


class TeamProjectLead(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile,)
    retired_key: ClassVar[str] = "commander_project"
    retired_field: ClassVar[str] = "commander"

    def carry(self, *, apply: bool) -> Carried:
        path = RegistryFile.path_in(self.root)
        self.require_confined(path)
        if not path.exists():
            return Carried(0)
        document = FieldCodec.decode(
            dict[str, JsonValue], FieldCodec.parse(path.read_text(encoding="utf-8")))
        version = FieldCodec.record_key(RegistryDocument, "version")
        if document[version] == RegistryDocument.version:
            return Carried(0)
        if self.retired_key in document:  # a bus without a team stored no project
            project = FieldCodec.decode(dict[str, JsonValue], document.pop(self.retired_key))
            project.pop(self.retired_field)
            document[FieldCodec.record_key(RegistryDocument, "team_project")] = project
        document[version] = RegistryDocument.version
        if apply:
            MaintenanceBarrier(path).read().require_held_by(
                MaintenanceHolder.current(), "Drop the stored team lead")
            RegistryStore(path).carry_text(json.dumps(document, indent=2))
        return Carried(1, rewritten=(path.relative_to(self.root),) if apply else ())
