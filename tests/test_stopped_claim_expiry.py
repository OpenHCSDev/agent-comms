"""A stopped registry incarnation loses every claim at the next original transition."""



from agent_comms.repo import Checkout
import os
from dataclasses import replace
from functools import partial

from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.comms import wire
from agent_comms.coordination_session import NativeBoundary
from agent_comms.envelope_claim_transitions import NativeClaim, WakeClaims
from agent_comms.field_codec import ClaimId, InputId, TurnId
from agent_comms.messages import ClaimNoticeKind
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread


def test_stopped_incarnation_claims_expire_on_next_transition(comms, tmp_path):
    turns = {}
    for name in ("old", "next"):
        turns[name] = TurnId.mint()
        thread = Thread(name, frozenset(), Checkout.from_wire(str(tmp_path)))
        comms.registry.declare(
            replace(thread, process_identity=ProcessIdentity.capture(os.getpid())),
            RunningThreadStatus(),
        )

    def acquire(name, resources, operation: ClaimId):
        with NativeBoundary(comms.bus) as boundary:
            owner = boundary.registry.require(name)
            scope = NativeClaim(
                owner_claimed_generation=boundary.registry.claim_generation(name),
                operation_id=operation, turn_id=turns[name], input_id=InputId.mint(),
            )
            return comms.bus.publisher._publish_claim_transition_unlocked(
                boundary.registry, ClaimNoticeKind.claimed(owner),
                partial(WakeClaims, incarnation=str(owner.created_at), claims=resources,
                        generation=operation, resource_scope=scope), activity=boundary.activity,
            )

    old_paths = tuple(str(tmp_path / name) for name in ("untouched.py", "other.py"))
    new_path = str(tmp_path / "next.py")
    acquire("old", old_paths, ClaimId("a" * 32))
    assert set(comms.bus.bus.claim_projection()) == set(old_paths)
    old = comms.registry.require("old")
    comms.registry.register(replace(old, process_identity=NoProcess()), StoppedThreadStatus())
    # The next transition touches neither old resource; both still have to disappear.
    committed = acquire("next", (new_path,), ClaimId("b" * 32))
    assert set(committed.expired) == set(old_paths)
    assert set(wire(comms.root).bus.bus.claim_projection()) == {new_path}
