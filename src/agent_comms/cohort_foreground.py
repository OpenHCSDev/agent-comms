"""Accept committed initial cohort rows addressed to one durable recipient."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_session import NativeBoundary
from agent_comms.coordinator import Coordination

from .cohort_schema import AcceptanceFrontier, CohortDeliveries, CohortTable
from .coordination_cohort import AcceptedCohort
from .cursor_owner import CursorOwner
from .errors import RelationViolationError
from .message_bus import MessageBus


async def _accept_visible_deliveries(
    bus: MessageBus,
    root_id: str,
    store_path: Path,
    lookup: str,
    *,
    owner_name: str,
    acquire_package: Callable[[], None] | None = None,
) -> None:
    """Accept only committed initial messages addressed to this registered recipient.

    Use the checked address index to find messages and decode only those not
    yet accepted. Every participant must already be registered. Public message
    text alone cannot identify the group receiving private input.
    """
    def accept(resource: Coordination) -> None:
        with NativeBoundary(bus) as boundary, bus.bus.reading() as source:
            boundary.privacy.require_root(root_id, IdentityConflict("native namespace changed"))
            with resource.session.read():
                db = resource.session._connection
                CohortTable.require_installed(db)
                thread = boundary.registry.require(owner_name)
                participant = resource.participants.get(lookup)
                cursor = CursorOwner(
                    thread=thread,
                    claimed_generation=boundary.registry.claim_generation(owner_name),
                    wire_root_id=root_id,
                    generation=participant.owner_identity.generation,
                ).state(db)
                after_seq = AcceptanceFrontier.scan_after(
                    db,
                    wire_root_id=root_id,
                    recipient_lookup=lookup,
                    covered_seq=cursor.covered_seq,
                )
                sealed = CohortDeliveries.sealed_sequences_after(
                    db,
                    wire_root_id=root_id,
                    recipient_lookup=lookup,
                    after_seq=after_seq,
                )
            originals = source.received(lookup, after=after_seq, limit=101, descending=False)
            addressed = tuple(original.seq for original in originals)
            # Progress comes from every addressed original in the fixed source
            # cut, including already sealed/historical recipient rows.
            unaccepted = []
            for initial in originals:
                if initial.seq in sealed:
                    continue
                recipients = [r for r in initial.audience.recipients if r.recipient_lookup == lookup]
                if not recipients:
                    raise RelationViolationError("checked initial lookup differs from bus row.")
                if any(r.canonical_thread == owner_name for r in recipients):
                    unaccepted.append(initial)
        visited_after = addressed[-1] if addressed else after_seq
        if len(unaccepted) > 100:
            raise IdentityConflict("recipient initial cohort batch exceeds bounded foreground scan")
        if unaccepted and acquire_package is not None:
            # An ACP observation with new originals must still validate the package
            # before SQL acceptance. Sealed evidences require no repeated preflight.
            acquire_package()
        # The frontier cannot outlive a rolled-back acceptance, nor does it
        # assert that any native input was consumed.
        with (
            NativeBoundary(bus) as boundary,
            bus.bus.reading() as source,
            resource.session.writing() as db,
        ):
            boundary.privacy.require_root(root_id, IdentityConflict("native namespace changed"))
            for initial in unaccepted:
                # Frozen recipients keep their original names after a rename.
                AcceptedCohort.record(source.delivery(initial.seq), boundary.privacy, resource, db)
            AcceptanceFrontier(
                wire_root_id=root_id,
                recipient_lookup=lookup,
                through_seq=visited_after,
            ).advance(db)

    await Coordination.run_async(store_path, accept)
