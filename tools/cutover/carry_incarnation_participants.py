"""Declare existing incarnations once; delete after the installed bus has carried them."""

from typing import ClassVar

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_tables.participants import Participants
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile, RootFile
from agent_comms.participant_store import ParticipantStore
from agent_comms.registry_store import RegistryStore


class IncarnationParticipants(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile, CoordinationStore)

    def carry(self, *, apply: bool) -> Carried:
        registry = RegistryFile.path_in(self.root)
        path = CoordinationStore.path_in(self.root)
        self.require_confined(self.root, registry)
        self.require_confined(self.root, path)
        with RegistryStore(registry).reading_snapshot() as snapshot:
            wanted = frozenset(thread.incarnation.require_recorded().lookup
                               for thread in snapshot.threads.values())
        existing: frozenset[str] = frozenset()
        if path.exists():
            with CoordinationStore.observing(path) as db:
                existing = frozenset(row.participant_lookup for row in Participants.select(db))
        missing = sorted(wanted - existing)
        if apply and missing:
            with CoordinationSession(str(path)) as session:
                participants = ParticipantStore(session)
                for lookup in missing:
                    participants.register(lookup, committed=True)
        return Carried(
            len(missing), detail={"incarnations": len(wanted), "missing": len(missing)},
            rewritten=(path.relative_to(self.root),) if apply and missing else (),
        )
