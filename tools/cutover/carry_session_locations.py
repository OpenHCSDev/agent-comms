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
from dataclasses import dataclass, field
from pathlib import Path

from agent_comms.field_codec import FieldCodec, compile_decoder
from agent_comms.historical_views import HistoryArchive, HistorySource
from agent_comms.installer.carry import Carried
from agent_comms.locked_store import RegistryFile
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.session_file import RegistrySessionLocation, SessionFile
from agent_comms.store_files import AtomicFile, _store_lock
from agent_comms.threads import ThreadProvenance
from agent_comms.typed_table import HeldBusFile

from .archived_roots import ArchivedCarry, CarryCatalogSource
from .carry_claim_fields import ClaimFields
from .carry_parked_threads import ParkedThreads


@dataclass
class Relocation:
    """Classify each old path string against the root that published its thread.

    FieldCodec walks records before their children, so an archived source names
    its original root before the threads it holds are rewritten.
    """

    published: Path
    carried: Counter[str] = field(default_factory=Counter)

    @staticmethod
    def selects(record: type) -> bool:
        return record is HistorySource or issubclass(record, ThreadProvenance)

    def change(self, record: type, raw: dict[str, object]) -> dict[str, object]:
        if record is HistorySource:
            self.published = Path(
                str(raw.get(FieldCodec.record_key(HistorySource, "original_root")))
            )
            return raw
        key = FieldCodec.record_key(ThreadProvenance, "session_file")
        saved = raw.get(key)
        if not isinstance(saved, str):
            return raw
        wire = FieldCodec.encode(
            RegistrySessionLocation.from_path(saved, self.published), SessionFile
        )
        self.carried[type(FieldCodec.decode(SessionFile, wire)).__name__] += 1
        return {**raw, key: wire}


class SessionLocations(ArchivedCarry):
    opened_files = (RegistryFile,)
    # Both decode registries with this source, and so do the steps they precede.
    precedes = (ClaimFields, ParkedThreads)

    def carry(self, *, apply: bool) -> Carried:
        if apply:
            HeldBusFile(RegistryFile, RegistryFile.path_in(self.root)).require()
        rewritten: list[Path] = []
        catalog = HistoryArchive.path_in(self.root)
        archived = Relocation(self.original_root)
        with _store_lock(catalog):
            recorded = json.loads(catalog.read_bytes()) if catalog.exists() else []
            current = compile_decoder(tuple[HistorySource, ...]).rewrite_document(
                recorded, archived.change, Relocation.selects
            )
            if archived.carried and not apply:
                # The catalogue names the archived roots and decodes only once carried.
                return Carried(archived.carried.total(), dict(archived.carried))
            if archived.carried:
                AtomicFile(catalog).write_text(json.dumps(current))
                rewritten.append(Path(HistoryArchive.filename))
            sources = CarryCatalogSource.read(self.root, self.original_root)
            registries = Relocation(self.original_root)
            for root in self.roots():
                registry = RegistryStore.at(root)
                if not registry.path.exists():
                    continue
                with registry.locked():
                    registry.private_guard_unlocked()
                    before = registries.carried.total()
                    raw = json.loads(registry.path.read_bytes())
                    carried = compile_decoder(RegistryDocument).rewrite_document(
                        raw, registries.change, Relocation.selects
                    )
                    if apply and registries.carried.total() != before:
                        registry._write_unlocked(json.dumps(carried, indent=2))
                        rewritten.extend(path.relative_to(self.root) for path in registry.written)
            if apply and rewritten:
                rebound = tuple(
                    source.carried_registry(source.mapped_root(self.root, self.original_root))
                    for source in sources
                )
                if rebound:
                    AtomicFile(catalog).write_text(json.dumps(FieldCodec.encode(rebound)))
                    rewritten.append(Path(HistoryArchive.filename))
        total = archived.carried + registries.carried
        return Carried(total.total(), dict(total), tuple(dict.fromkeys(rewritten)))
