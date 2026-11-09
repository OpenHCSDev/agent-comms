"""Copy registry.json's threads, statuses, aliases and generations into registry.sqlite3's rows.

The whole-document registry rewrote every row on every change (5.6 MB and 2,933 threads on the
live bus to change one); RegistryRowStore writes the rows a change changes. This step writes every
stored row once, at revision 1, and does nothing on a store that already has a revision. Rows a
current Thread no longer declares a key for (a retired field) decode without it and are counted.
Delete this step after the live installation.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import JsonValue

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.registry.registry_document import RegistryDocument, RootPurpose, TeamProject
from agent_comms.registry.registry_tables import (
    RegistryAliases,
    RegistryClaimGenerations,
    RegistryHeader,
    RegistryOwnerGenerations,
    RegistryRowStore,
    RegistryStatuses,
    RegistryThreads,
)
from agent_comms.registry.thread_identity import GenerationCounter
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import Thread
from agent_comms.storage.locked_store import RegistryFile, RootFile
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder


class RegistryRows(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile, RegistryRowStore)

    def carry(self, *, apply: bool) -> Carried:
        path = RegistryFile.path_in(self.root)
        self.require_confined(path)
        store = RegistryRowStore(self.root)
        self.require_confined(store.path)
        if not path.exists() or store.revision():
            return Carried(0)
        document = FieldCodec.decode(
            dict[str, JsonValue], FieldCodec.parse(path.read_text(encoding="utf-8")))
        if apply:
            MaintenanceBarrier(path).read().require_held_by(
                MaintenanceHolder.current(), "Copy the registry's rows into registry.sqlite3")
            store.commit(self.header(document), self.rows(document), [])
        return Carried(1, rewritten=(store.path.relative_to(self.root),) if apply else ())

    @staticmethod
    def field(document: dict[str, JsonValue], name: str) -> JsonValue:
        return document.get(FieldCodec.record_key(RegistryDocument, name), {})

    def header(self, document: dict[str, JsonValue]) -> RegistryHeader:
        owners = FieldCodec.decode(GenerationCounter, self.field(document, "owners"))
        claims = FieldCodec.decode(GenerationCounter, self.field(document, "claims"))
        return RegistryHeader(
            header=1, revision=0,
            purpose=FieldCodec.decode(RootPurpose, self.field(document, "purpose")),
            team_project=FieldCodec.decode(TeamProject, self.field(document, "team_project")),
            owner_counter=owners.counter, claim_counter=claims.counter,
        )

    def rows(self, document: dict[str, JsonValue]):
        threads = FieldCodec.decode(dict[str, JsonValue], self.field(document, "threads"))
        statuses = FieldCodec.decode(dict[str, JsonValue], self.field(document, "statuses"))
        aliases = FieldCodec.decode(dict[str, str], self.field(document, "aliases"))
        owners = FieldCodec.decode(GenerationCounter, self.field(document, "owners"))
        claims = FieldCodec.decode(GenerationCounter, self.field(document, "claims"))
        for name, thread in threads.items():
            yield RegistryThreads(changed_at=1, name=name, thread=FieldCodec.decode(Thread, thread))
        for name, status in statuses.items():
            yield RegistryStatuses(changed_at=1, name=name, status=FieldCodec.decode(ThreadStatus, status))
        for alias, name in aliases.items():
            yield RegistryAliases(changed_at=1, alias=alias, name=name)
        for name, generation in owners.generations.items():
            yield RegistryOwnerGenerations(changed_at=1, name=name, generation=generation)
        for name, generation in claims.generations.items():
            yield RegistryClaimGenerations(changed_at=1, name=name, generation=generation)
