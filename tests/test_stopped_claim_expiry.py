"""A stopped registry incarnation loses every claim at the next original transition."""

import os
from dataclasses import replace
from functools import partial

from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_session import NativeBoundary
from agent_comms.envelope_claim_transitions import NativeClaim, WakeClaims
from agent_comms.messages import ClaimNoticeKind
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus


def test_stopped_incarnation_claims_expire_on_next_transition(comms, tmp_path):
    comms.messaging.initialize_private_initial_protocol()
    for name in ("old", "next"):
        thread = comms.registry.new_thread(name, frozenset(), str(tmp_path))
        comms.registry.declare(
            replace(thread, process_identity=ProcessIdentity.capture(os.getpid())),
            RunningThreadStatus(),
        )

    def acquire(name, resources, operation):
        with NativeBoundary(comms.bus) as boundary:
            owner = boundary.registry.require(name)
            scope = NativeClaim(
                owner_claimed_generation=boundary.registry.claim_generation(name),
                operation_id=operation, turn_id=f"turn-{name}", input_id=f"input-{name}",
            )
            return comms.bus.publisher._publish_claim_transition_unlocked(
                boundary.bus_lock, boundary.registry, ClaimNoticeKind.claimed(owner),
                partial(WakeClaims, incarnation=str(owner.created_at), claims=resources,
                        generation=operation, claim_scope=scope), activity=boundary.activity,
            )

    old_paths = tuple(str(tmp_path / name) for name in ("untouched.py", "other.py"))
    new_path = str(tmp_path / "next.py")
    acquire("old", old_paths, "a" * 32)
    assert set(comms.bus.log.claim_projection()) == set(old_paths)
    old = comms.registry.require("old")
    comms.registry.register(replace(old, process_identity=NoProcess()), StoppedThreadStatus())
    # The next transition touches neither old resource; both still have to disappear.
    committed = acquire("next", (new_path,), "b" * 32)
    assert set(committed.expired) == set(old_paths)
    assert set(Comms(comms.root).bus.log.claim_projection()) == {new_path}
