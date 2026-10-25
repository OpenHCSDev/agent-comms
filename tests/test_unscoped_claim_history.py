"""Bus history holds tool acquisitions written before claims recorded a scope."""

from agent_comms.repo import Checkout
import pytest

from agent_comms.comms import wire
from agent_comms.envelope_claim_transitions import (
    ClaimProjection,
    ClaimRelease,
    ClaimTransition,
    NativeClaim,
    ReleasedClaims,
    UnrecordedScope,
    UnscopedClaims,
    WakeClaims,
)
from agent_comms.field_codec import ClaimId, FieldCodec, InputId, MalformedRecord, MessageId, TurnId
from agent_comms.thread_status import RunningThreadStatus
from agent_comms.threads import Thread

G1, G2 = ClaimId("a" * 32), ClaimId("b" * 32)


def test_unscoped_history_row_decodes_projects_never_live_and_releases(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.registry.declare(Thread("owner", frozenset(), Checkout.from_wire(str(tmp_path))))
    comms.registry.register(comms.registry.require("owner"), RunningThreadStatus())
    comms.registry.declare(Thread("peer", frozenset(), Checkout.from_wire(str(tmp_path))))
    owner = comms.registry.require("owner")
    resource = str(tmp_path / "held.py")
    message_id = MessageId.mint("msg-1")
    next_message_id = MessageId.mint("msg-2")
    # The row exactly as the writer before native claim scopes stored it.
    stored = {"owner": "owner", "incarnation": str(owner.created_at), "seq": 1,
              "message_id": FieldCodec.encode(message_id), "claims": [resource],
              "releases": [], "generation": G1}

    transition = FieldCodec.decode(ClaimTransition, stored)
    assert type(transition) is UnscopedClaims
    assert FieldCodec.encode(transition) == stored
    transition.require_new_operation(lambda operation: pytest.fail("no operation was recorded"))
    held = transition.project(ClaimProjection())
    assert held[resource].resource_scope == UnrecordedScope()
    registry = comms.registry.snapshot()
    assert not held[resource].live(registry)

    # A current acquisition by another owner is not blocked by the unscoped holder.
    peer = registry.require("peer")
    taken = WakeClaims(
        owner="peer",
        incarnation=str(peer.created_at),
        seq=2,
        message_id=next_message_id,
        claims=(resource,),
        generation=G2,
        resource_scope=NativeClaim(
            owner_claimed_generation=1,
            operation_id=ClaimId("c" * 32),
            turn_id=TurnId('ff33c94032d9f01447a779689c1dff6a'),
            input_id=InputId.mint(),
        ),
    ).apply(held, registry)
    assert taken[resource].owner == "peer"

    # The owner's recorded release of its unscoped generation still replays.
    released = ReleasedClaims(owner="owner", incarnation=str(owner.created_at), seq=2,
                              message_id=next_message_id, releases=(ClaimRelease(resource, G1),))
    assert dict(released.project(held)) == {}

    # A row published today records its scope; history rows carry none.
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(ClaimTransition, {**stored, "resource_scope": None})
    scoped = FieldCodec.encode(taken[resource])
    assert scoped["resource_scope"]["operation_id"] == "c" * 32
