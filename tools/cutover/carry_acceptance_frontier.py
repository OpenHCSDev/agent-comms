"""Seed acceptance progress once without skipping holes or replaying native input."""

from typing import ClassVar

from agent_comms.bus import Bus
from agent_comms.bus_tables import ReceivedSource
from agent_comms.cohort_schema import (
    AcceptanceFrontier,
    ClaimBatchAcknowledgements,
    CohortDeliveries,
)
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_tables.participants import Participants
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile, RootFile
from agent_comms.native_runtime_input import CurrentNativeCursor
from agent_comms.private_registry_guard import PrivateRegistryGuard, RegistryPrivacy


class AcceptanceFrontiers(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (
        CoordinationStore,
        Bus,
        RegistryFile,
        PrivateRegistryGuard,
    )

    def carry(self, *, apply: bool) -> Carried:
        path = CoordinationStore.path_in(self.root)
        source = Bus.path_in(self.root)
        self.require_confined(self.root, path)
        self.require_confined(self.root, source)
        if not path.exists():
            return Carried(0)
        with CoordinationStore.observing(path) as db:
            if not CohortDeliveries.present(db):
                return Carried(0)  # Public roots have no native acceptance state.
            root_id = RegistryPrivacy.capture(self.root).namespace
            existing = (
                {
                    row.recipient_lookup
                    for row in AcceptanceFrontier.select(db, wire_root_id=root_id)
                }
                if AcceptanceFrontier.present(db)
                else set()
            )
            missing = [
                row.participant_lookup
                for row in Participants.select(db)
                if row.participant_lookup not in existing
            ]
            if not missing:
                return Carried(0)
            covered: dict[str, int] = dict(
                db.execute(
                    f"SELECT recipient_lookup, MAX(covered_seq) FROM {CurrentNativeCursor.declared_name} "
                    "WHERE wire_root_id=? GROUP BY recipient_lookup",
                    (root_id,),
                )
            )
            db.execute("ATTACH DATABASE ? AS addressed", (source.as_uri() + "?mode=ro",))
            frontiers: list[AcceptanceFrontier] = []
            for lookup in missing:
                after_seq = covered.get(lookup, 0)
                through_seq = db.execute(
                    "SELECT COALESCE(MIN(CASE WHEN sealed.wire_seq IS NULL THEN r.seq END)-1, "
                    "MAX(r.seq), ?) "
                    f"FROM addressed.{ReceivedSource.declared_name} r LEFT JOIN ("
                    f"SELECT d.wire_seq FROM {CohortDeliveries.declared_name} d "
                    f"JOIN {ClaimBatchAcknowledgements.declared_name} b "
                    "ON b.wire_root_id=d.wire_root_id AND b.wire_seq=d.wire_seq AND b.sealed=1 "
                    "WHERE d.wire_root_id=? AND d.recipient_lookup=?) sealed "
                    "ON sealed.wire_seq=r.seq WHERE r.lookup=? AND r.seq>?",
                    (after_seq, root_id, lookup, lookup, after_seq),
                ).fetchone()[0]
                frontiers.append(
                    AcceptanceFrontier(
                        wire_root_id=root_id,
                        recipient_lookup=lookup,
                        through_seq=through_seq,
                    )
                )
        if apply:
            # Opening this declared writer reconciles its new table under the install hold.
            with CoordinationSession(str(path)) as session, session.writing() as db:
                for frontier in frontiers:
                    frontier.insert_if_absent(db)
        return Carried(
            len(frontiers),
            detail={"recipients": len(frontiers)},
            rewritten=(path.relative_to(self.root),) if apply else (),
        )
