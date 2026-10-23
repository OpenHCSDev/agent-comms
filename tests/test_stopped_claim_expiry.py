"""A stopped registry incarnation loses every claim at the next original transition."""


from agent_comms.bus import Bus

import os
from dataclasses import replace
from functools import partial

from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.comms import wire
from agent_comms.coordination_session import NativeBoundary
from agent_comms.envelope_claim_transitions import NativeClaim, WakeClaims
from agent_comms.messages import ClaimNoticeKind
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus
from agent_comms.threads import Thread


def test_stopped_incarnation_claims_expire_on_next_transition(comms, tmp_path):
    Bus.install(Bus.path_in(comms.root))
    for name in ("old", "next"):
        thread = Thread(name, frozenset(), str(tmp_path))
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
                boundary.registry, ClaimNoticeKind.claimed(owner),
                partial(WakeClaims, incarnation=str(owner.created_at), claims=resources,
                        generation=operation, resource_scope=scope), activity=boundary.activity,
            )

    old_paths = tuple(str(tmp_path / name) for name in ("untouched.py", "other.py"))
    new_path = str(tmp_path / "next.py")
    acquire("old", old_paths, "a" * 32)
    assert set(comms.bus.bus.claim_projection()) == set(old_paths)
    old = comms.registry.require("old")
    comms.registry.register(replace(old, process_identity=NoProcess()), StoppedThreadStatus())
    # The next transition touches neither old resource; both still have to disappear.
    committed = acquire("next", (new_path,), "b" * 32)
    assert set(committed.expired) == set(old_paths)
    assert set(wire(comms.root).bus.bus.claim_projection()) == {new_path}
