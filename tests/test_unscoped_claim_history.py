"""Bus history holds tool acquisitions written before claims recorded a scope."""
import pytest

from agent_comms.comms import Comms
from agent_comms.envelope_claim_transitions import (
    ClaimProjection, ClaimRelease, ClaimTransition, NativeClaim, ReleasedClaims,
    UnrecordedScope, UnscopedClaims, WakeClaims,
)
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.thread_status import RunningThreadStatus

G1, G2 = "a" * 32, "b" * 32


def test_unscoped_history_row_decodes_projects_never_live_and_releases(tmp_path):
    comms = Comms(tmp_path / "wire")
    comms.registry.declare(comms.registry.new_thread("owner", frozenset(), str(tmp_path)))
    comms.registry.register(comms.registry.require("owner"), RunningThreadStatus())
    comms.registry.declare(comms.registry.new_thread("peer", frozenset(), str(tmp_path)))
    owner = comms.registry.require("owner")
    resource = str(tmp_path / "held.py")
    # The row exactly as the writer before native claim scopes stored it.
    stored = {"owner": "owner", "incarnation": str(owner.created_at), "seq": 1,
              "message_id": "msg-1", "claims": [resource], "releases": [], "generation": G1}

    transition = FieldCodec.decode(ClaimTransition, stored)
    assert type(transition) is UnscopedClaims
    assert FieldCodec.encode(transition) == stored
    transition.require_new_operation(lambda operation: pytest.fail("no operation was recorded"))
    held = transition.project(ClaimProjection())
    assert held[resource].claim_scope == UnrecordedScope()
    registry = comms.registry.snapshot()
    assert not held[resource].live(registry)

    # A current acquisition by another owner is not blocked by the unscoped holder.
    peer = registry.require("peer")
    taken = WakeClaims(
        owner="peer", incarnation=str(peer.created_at), seq=2, message_id="msg-2",
        claims=(resource,), generation=G2,
        claim_scope=NativeClaim(owner_claimed_generation=1, operation_id="c" * 32,
                                turn_id="turn-2", input_id="input-2"),
    ).apply(held, registry)
    assert taken[resource].owner == "peer"

    # The owner's recorded release of its unscoped generation still replays.
    released = ReleasedClaims(owner="owner", incarnation=str(owner.created_at), seq=2,
                              message_id="msg-2", releases=(ClaimRelease(resource, G1),))
    assert dict(released.project(held)) == {}

    # A row published today records its scope; history rows carry none.
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(ClaimTransition, {**stored, "admission": None})
    scoped = FieldCodec.encode(taken[resource])
    assert scoped["admission"]["operation_id"] == "c" * 32
