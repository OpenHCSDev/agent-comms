"""Carry presence JSON into keyed rows once; remove this step after installation."""

from pathlib import Path
from typing import ClassVar

from agent_comms.execution.child_process import Platform
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.presence import PresenceStore
from agent_comms.storage.locked_store import DurableFile, RootFile
from agent_comms.storage.store_files import DependentStateTier


class JsonPresenceFile(DurableFile, DependentStateTier):
    """The outgoing file, declared only for this carry's backup and retirement."""

    filename: ClassVar[str] = "presence.json"


class KeyedPresence(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (JsonPresenceFile, PresenceStore)

    def sources(self) -> tuple[Path, ...]:
        """Include presence belonging to declared retained registry roots."""
        return tuple(sorted(path for path, owner in RootFile.primaries_in(self.root).items()
                            if issubclass(owner, JsonPresenceFile)))

    def input_files(self) -> tuple[Path, ...]:
        sources = self.sources()
        paths = {*super().input_files(), *sources}
        paths.update(PresenceStore.path_in(path.parent) for path in sources)
        for path in paths:
            self.require_confined(path)
        return tuple(sorted(paths))

    def carry(self, *, apply: bool) -> Carried:
        written = []
        sources = self.sources()
        rows = 0
        for path in sources:
            self.require_confined(path)
            seen = FieldCodec.decode(dict[str, float], FieldCodec.parse(path.read_bytes()))
            rows += len(seen)
            if apply:
                store = PresenceStore(path.parent)
                self.require_confined(store.path)
                before = store.read()
                store.set_rows(seen, before.seen)
                after = store.read(before)
                if any(after.seen[name] != when for name, when in seen.items()):
                    raise ValueError("Presence rows differ from their carried timestamps")
                if after.revision != before.revision:
                    written.append(store.path.relative_to(self.root))
                path.unlink()
                Platform.current().sync_directory(path.parent)
                written.append(path.relative_to(self.root))
        return Carried(len(sources), detail={"rows": rows}, rewritten=tuple(written))
