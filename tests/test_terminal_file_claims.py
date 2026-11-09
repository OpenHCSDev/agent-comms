"""Exact terminal claim release on real private stores and worker processes."""

from dataclasses import replace

from agent_comms.execution.selected.claim import publish_selected_resource_claim
from agent_comms.messaging.envelope_claim_transitions import ExistingFileClaim
from agent_comms.nominal.field_codec import ClaimId
from test_claim_bus_read import claim_originals, settle_failed

pytest_plugins = ("test_claim_bus_read",)


def test_terminal_release_spans_the_existing_envelope_bound(selected_turn):
    (comms, store, selected, resource) = selected_turn
    for index in range(35):
        file = resource.parent / f"file-{index}.py"
        file.write_text("original\n")
        publish_selected_resource_claim(
            comms, store, replace(selected, operation_id=ClaimId(f"{index:032x}")), "Alice",
            ExistingFileClaim(file),
        )
    settle_failed(store)
    assert dict(comms.bus.bus.claim_projection()) == {}
    notices = [m for m in claim_originals(comms) if m.claim_transition.releases]
    releases = [m.claim_transition for m in notices]
    assert [len(row.releases) for row in releases] == [32, 3]
    assert sum(m.body.count("file-") for m in notices) == 35
