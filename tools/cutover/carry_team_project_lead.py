"""Drop the stored lead from the registry, whose project now belongs to the team.

The registry kept the project under ``commander_project`` with a ``commander`` incarnation:
one thread recorded as the team's lead. The project is now ``team_project`` with only its
repositories, the teammates' channel tag is ``teammates`` and a fork runs a plain
``fork_execution``. The version-2 decoder refuses version 1, so this step stamps every
registry version 2, moving a stored project to its new key without its lead and renaming those
tags as TeamVocabulary renames them everywhere else. A thread is now its execution, so every
row names it: a native row, which omitted the default, is stamped native. Delete this step
after the live installation.
"""

from __future__ import annotations

import json
from typing import ClassVar

from cutover.carry_team_vocabulary import TeamVocabulary
from pydantic import JsonValue

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.registry_document import RegistryDocument
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.registry.threads import NativeThread, Thread
from agent_comms.storage.locked_store import RegistryFile, RootFile
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder


class TeamProjectLead(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile,)
    retired_key: ClassVar[str] = "commander_project"
    retired_field: ClassVar[str] = "commander"
    stamped_version: ClassVar[int] = 2
    """The registry version this step writes. Later steps raise it further, so a registry at
    this version or above is already carried."""

    def carry(self, *, apply: bool) -> Carried:
        path = RegistryFile.path_in(self.root)
        self.require_confined(path)
        if not path.exists():
            return Carried(0)
        document = FieldCodec.decode(
            dict[str, JsonValue], FieldCodec.parse(path.read_text(encoding="utf-8"))
        )
        version = FieldCodec.record_key(RegistryDocument, "version")
        if FieldCodec.decode(int, document[version]) >= self.stamped_version:
            return Carried(0)
        if self.retired_key in document:  # a bus without a team stored no project
            project = FieldCodec.decode(dict[str, JsonValue], document.pop(self.retired_key))
            project.pop(self.retired_field)
            document[FieldCodec.record_key(RegistryDocument, "team_project")] = project
        document[version] = self.stamped_version
        rows = FieldCodec.record_key(RegistryDocument, "threads")
        native: dict[str, JsonValue] = {Thread.family_discriminator: NativeThread.declared_name}
        stamped: dict[str, JsonValue] = {
            name: {**native, **row}
            for name, row in FieldCodec.decode(
                dict[str, dict[str, JsonValue]], document[rows]
            ).items()
        }
        document[rows] = stamped
        registry = RegistryStore(path)
        if apply:
            MaintenanceBarrier(path).read().require_held_by(
                MaintenanceHolder.current(), "Drop the stored team lead"
            )
            registry.carry_text(TeamVocabulary.renamed_text(json.dumps(document, indent=2)))
        return Carried(
            1,
            rewritten=(
                tuple(written.relative_to(self.root) for written in registry.written)
                if apply
                else ()
            ),
        )
