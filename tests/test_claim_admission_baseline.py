"""Characterize the CURRENT separation of N/K wake and resource claims.

This is not an admission bridge or an assertion that an unselected observer
should be able to edit another worker's file. The follow-up must gate the
native write boundary; independent explicit file claims still have a use.
No model/provider is started by this test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordination_cohort import accept_initial_cohort, sealed_cohort_claims
from agent_comms.coordination_store import MutationStore
from agent_comms.declarations import Thread
from agent_comms.operations import Comms

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="private N/K and claim durability require a real /var/tmp root"
)


def test_selected_wake_and_file_claims_have_no_common_admission_receipt() -> None:
    with TemporaryDirectory(prefix="ac-claim-admission-", dir="/var/tmp") as dirname:
        root = Path(dirname) / "wire"
        root.mkdir(mode=0o700)
        worktree = Path(dirname) / "work"
        worktree.mkdir(mode=0o700)
        resource = worktree / "module.py"
        resource.write_text("value = 1\n")
        comms = Comms(root)
        people = {"sender": 17001.0, "Alice": 17002.0, "Bob": 17003.0}
        for name, created_at in people.items():
            comms.register(
                Thread(
                    name,
                    frozenset({"team"}),
                    str(worktree),
                    created_at=created_at,
                )
            )
        root_id = comms.initialize_private_initial_protocol()
        # BOTH barriers must be installed while the private bus is empty.
        comms.initialize_private_claim_protocol()
        with MutationStore(str(root / "coordination.sqlite3")) as coordinator:
            install_private_cohort_schema(coordinator)
            for name in ("Alice", "Bob"):
                coordinator.register_participant(
                    stable_thread_lookup(people[name]), name, name, committed=True
                )
            message = comms.send_initial_cohort("sender", "#team", "Please investigate @Alice")
            receipt = accept_initial_cohort(comms.bus, root_id, message.seq, coordinator).value
            assert receipt.member_count == 2
            assert receipt.claim_count == 1
            assert sealed_cohort_claims(coordinator, stable_thread_lookup(people["Alice"]))
            assert sealed_cohort_claims(coordinator, stable_thread_lookup(people["Bob"])) == ()

            # Existing explicit resource claims are valid independent operations.
            # They neither consult nor reference the sealed N/K receipt. A
            # future native edit admission must join these two authorities.
            committed = comms.send_message(
                "Bob", "#team", "Independent file claim", claims=["module.py"]
            )
            assert committed.claim_transition is not None
            assert not hasattr(committed.claim_transition, "wake_claim_id")
            projection = comms.claim_projection()
            assert projection[str(resource)].owner == "Bob"
            assert resource.read_text() == "value = 1\n"
