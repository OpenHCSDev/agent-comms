"""Accept committed initial cohort rows addressed to one durable recipient."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination

from .cohort_schema import CohortDeliveryReceipts, assert_cohort_schema
from .coordination_cohort import accept_delivery_cohort
from .errors import RelationViolationError
from .message_bus import MessageBus
from .private_registry_guard import _require_no_private_owner_rename


async def _accept_visible_deliveries(
    bus: MessageBus,
    root_id: str,
    store_path: Path,
    lookup: str,
    after_seq: int,
    *,
    owner_name: str,
    acquire_package: Callable[[], None] | None = None,
) -> int:
    """Accept only committed initial rows addressed to this durable recipient.

    Membership comes from the certified addressed index; only unsealed rows are
    captured and decoded. All N identities must already be registered. Never
    infer a cohort from an ordinary public message or its body.
    """
    def accept(resource: Coordination) -> int:
        with bus.log.certified_read() as source:
            _require_no_private_owner_rename(bus.log.path.parent)
            marker = bus.log._private_marker_unlocked()
            if marker.root_id != root_id:
                raise IdentityConflict("private initial wire root changed")
            admitted_after = max(after_seq, marker.admission_after_seq)
            with resource.session.read():
                assert_cohort_schema(resource.session._connection)
                sealed = CohortDeliveryReceipts.sealed_sequences_after(
                    resource.session._connection,
                    wire_root_id=root_id,
                    recipient_lookup=lookup,
                    after_seq=admitted_after,
                )
            addressed = source.addressed_sequences(bus.log, lookup, admitted_after)
            # Progress comes from every addressed original in the fixed source
            # cut, including already sealed/historical recipient rows.
            unaccepted = []
            for seq in addressed:
                if seq in sealed:
                    continue
                initial = source.delivery(seq)
                recipients = [r for r in initial.audience.recipients if r.recipient_lookup == lookup]
                if not recipients:
                    raise RelationViolationError("Certified initial lookup differs from bus row.")
                if any(r.canonical_thread == owner_name for r in recipients):
                    unaccepted.append(initial)
        visited_after = addressed[-1] if addressed else admitted_after
        if len(unaccepted) > 100:
            raise IdentityConflict("recipient initial cohort batch exceeds bounded foreground scan")
        if unaccepted and acquire_package is not None:
            # An ACP observation with new originals must still validate the package
            # before SQL acceptance. Sealed receipts require no repeated preflight.
            acquire_package()
        for initial in unaccepted:
            # A prior canonical name is historical after a private owner rename.
            # Never create a NEW generation's selected attempt from that old
            # frozen recipient, or infer it was consumed.
            accept_delivery_cohort(bus, root_id, initial.message.seq, resource)
        return visited_after

    return await Coordination.run_async(store_path, accept)
