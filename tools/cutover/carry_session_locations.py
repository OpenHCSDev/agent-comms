"""Store each saved transcript as a bus location or an external one, once.

The registry stored every saved transcript as one absolute path string, so a
copied root read and wrote the transcripts of the bus it was copied from. A path
under the bus that published it becomes a location relative to the root that
reads it; any other path is an external harness transcript, which only the
published root may write. Delete this step after every retained root has run it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import HistoryArchive
from agent_comms.installer.carry import Carried
from agent_comms.locked_store import RegistryFile
from agent_comms.registry_store import RegistryStore
from agent_comms.session_file import RegistrySessionLocation, SessionFile
from agent_comms.store_files import AtomicFile, _store_lock
from agent_comms.typed_table import HeldBusFile

from .archived_roots import ArchivedCarry, CarryCatalogSource
from .carry_claim_fields import ClaimFields
from .carry_parked_threads import ParkedThreads


class SessionLocations(ArchivedCarry):
    opened_files = (RegistryFile,)
    # Both decode registries with this source, and so do the steps they precede.
    precedes = (ClaimFields, ParkedThreads)

    def carry(self, *, apply: bool) -> Carried:
        if apply:
            HeldBusFile(RegistryFile, RegistryFile.path_in(self.root)).require()
        rewritten: list[Path] = []
        carried: Counter[str] = Counter()
        catalog = HistoryArchive.path_in(self.root)
        with _store_lock(catalog):
            recorded = json.loads(catalog.read_bytes()) if catalog.exists() else []
            for source in recorded:
                carried.update(self.carry_threads(
                    source["provenance"]["threads"].values(), Path(source["original_root"])))
            if carried and not apply:
                # The catalogue names the archived roots and decodes only once carried.
                return Carried(carried.total(), dict(carried))
            if carried:
                AtomicFile(catalog).write_text(json.dumps(recorded))
                rewritten.append(Path(HistoryArchive.filename))
            sources = CarryCatalogSource.read(self.root, self.original_root)
            for root in self.roots():
                registry = RegistryStore.at(root)
                if not registry.path.exists():
                    continue
                with registry.locked():
                    registry.private_guard_unlocked()
                    raw = json.loads(registry.path.read_bytes())
                    changed = self.carry_threads(raw["threads"].values(), self.original_root)
                    carried.update(changed)
                    if changed and apply:
                        registry._write_unlocked(json.dumps(raw, indent=2))
                        rewritten.extend(path.relative_to(self.root) for path in registry.written)
            if apply and rewritten:
                rebound = tuple(source.carried_registry(source.mapped_root(self.root, self.original_root))
                                for source in sources)
                if rebound:
                    AtomicFile(catalog).write_text(json.dumps(FieldCodec.encode(rebound)))
                    rewritten.append(Path(HistoryArchive.filename))
        return Carried(carried.total(), dict(carried), tuple(dict.fromkeys(rewritten)))

    @staticmethod
    def carry_threads(threads, published: Path) -> Counter[str]:
        """Rewrite each old path string in place; the decoded member names the result."""
        changed: Counter[str] = Counter()
        for thread in threads:
            saved = thread.get("session_file")
            if not isinstance(saved, str):
                continue
            location = RegistrySessionLocation.from_path(saved, published)
            thread["session_file"] = FieldCodec.encode(location, SessionFile)
            changed[type(FieldCodec.decode(SessionFile, thread["session_file"])).__name__] += 1
        return changed
