"""Move the retired commander file's identity and repositories into the registry.

The registry is authoritative once configured. A leftover input never overwrites
a newer native project. Delete this carry after every retained root has crossed.
"""

from __future__ import annotations

from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile
from agent_comms.registry_document import NoCommanderProject, RegisteredCommanderProject
from agent_comms.registry_store import RegistryStore
from agent_comms.repo import Repo
from agent_comms.typed_table import HeldBusFile


class CommanderProjectCarry(Carry):
    @property
    def configuration(self) -> Path:
        return self.root / "commander.json"

    def backup_files(self) -> tuple[Path, ...]:
        paths = (self.configuration, *RegistryStore.at(self.root).written)
        for path in paths:
            self.require_confined(self.root, path)
        return tuple(path for path in paths if path.exists())

    def carry(self, *, apply: bool) -> Carried:
        self.require_confined(self.root, self.configuration)
        if not self.configuration.exists():
            return Carried(0)
        registry = RegistryStore.at(self.root)
        snapshot = registry.read_snapshot_unlocked()
        project = snapshot.commander_project
        if isinstance(project, NoCommanderProject):
            raw = FieldCodec.decode(
                dict[str, object], FieldCodec.parse(self.configuration.read_text())
            )
            commander = snapshot.require(FieldCodec.decode(str, raw["commander"]))
            project = RegisteredCommanderProject(
                commander.incarnation, FieldCodec.decode(tuple[Repo, ...], raw["repos"])
            )
        if not apply:
            return Carried(1)
        HeldBusFile(RegistryFile, registry.path).require()
        rewritten = []
        with registry.editing() as edit:
            if isinstance(edit.document.commander_project, NoCommanderProject):
                edit.document.set_commander_project(project)
                edit.commit()
                rewritten.extend(registry.written)
        self.configuration.unlink()
        rewritten.append(self.configuration)
        return Carried(1, rewritten=tuple(str(path.relative_to(self.root)) for path in rewritten))
