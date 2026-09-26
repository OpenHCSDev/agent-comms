"""Sealed N/K admission checks against real private bus, registry, and SQLite stores."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.claim_admission import (
    publish_selected_resource_claim,
    verify_selected_wake,
    write_selected_claimed_file,
)
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime import _engage
from agent_comms.coordination import AttemptPhase, ClaimDisposition
from agent_comms.coordination_cohort import accept_initial_cohort, sealed_cohort_claims
from agent_comms.coordination_store import IdentityConflict, MutationStore, prepare_fence_token
from agent_comms.declarations import ClaimEnvelopeUnknownError, Thread
from agent_comms.envelope_claim_transitions import WakeAdmission
from agent_comms.operations import Comms

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="private cohort admission requires real /var/tmp"
)


def test_selected_wake_verifier_refuses_no_wake_and_stale_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory(prefix="ac-claim-verify-", dir="/var/tmp") as dirname:
        root = Path(dirname) / "wire"
        root.mkdir(mode=0o700)
        worktree = Path(dirname) / "work"
        worktree.mkdir(mode=0o700)
        resource = worktree / "module.py"
        resource.write_text("value = 1\n")
        comms = Comms(root)
        for name, created in (("sender", 17021.0), ("Alice", 17022.0), ("Bob", 17023.0)):
            comms.register(
                Thread(
                    name, frozenset({"team"}), str(worktree), pid=os.getpid(), created_at=created
                )
            )
        root_id = comms.initialize_private_initial_protocol()
        comms.initialize_private_claim_protocol()
        message = comms.send_initial_cohort("sender", "#team", "@Alice investigate")
        initial = comms.bus.read_initial_cohort(root_id, message.seq)
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            for recipient in initial.audience.recipients:
                store.register_participant(
                    recipient.recipient_lookup,
                    recipient.canonical_thread,
                    recipient.canonical_thread,
                    committed=True,
                )
            accept_initial_cohort(comms.bus, root_id, message.seq, store)
            alice_lookup = stable_thread_lookup(comms.registry.require("Alice").created_at)
            bob_lookup = stable_thread_lookup(comms.registry.require("Bob").created_at)
            assert sealed_cohort_claims(store, bob_lookup) == ()
            claim = sealed_cohort_claims(store, alice_lookup)[0]
            assert claim.disposition is ClaimDisposition.FULL_PENDING
            owner, admission_generation = comms.registry.live_owner_with_admission("Alice")
            owner, admission_generation = comms.registry.claim_live_turn_with_admission(
                owner, "selected-turn", expected_generation=admission_generation
            )
            person = store.participant(alice_lookup)
            execution_id = _engage(store, claim, initial, owner, person.generation)
            snapshot = store.snapshot(execution_id)
            snapshot = store.mark_pending(
                execution_id, expected_revision=snapshot.execution.revision
            ).value
            started = store.start_attempt(
                execution_id,
                1,
                owner.name,
                person.generation,
                prepare_fence_token(),
                expected_execution_revision=snapshot.execution.revision,
                expected_pointer_revision=snapshot.pointer_revision,
            )
            engaged = store.claim(claim.claim_id)
            admission = WakeAdmission(
                wire_root_id=root_id,
                source_seq=message.seq,
                source_message_id=message.message_id,
                wake_claim_id=claim.claim_id,
                wake_revision=engaged.revision,
                recipient_lookup=alice_lookup,
                execution_id=execution_id,
                operation_id="f" * 32,
                owner_admission_generation=admission_generation,
                turn_id=owner.active_turn.id,
                participant_generation=person.generation,
                attempt_ordinal=1,
            )
            verify_selected_wake(comms, store, admission, owner.name)
            with pytest.raises(IdentityConflict):
                publish_selected_resource_claim(
                    comms, store, replace(admission, recipient_lookup=bob_lookup), "Bob", resource
                )
            assert comms.claim_projection().get(str(resource)) is None
            append = comms.bus._append_private_unlocked

            def append_then_lose_receipt(metadata: dict[str, int | str], row: dict) -> None:
                append(metadata, row)
                raise OSError("lost acknowledgement after durable append")

            with monkeypatch.context() as patch:
                patch.setattr(comms.bus, "_append_private_unlocked", append_then_lose_receipt)
                with pytest.raises(ClaimEnvelopeUnknownError):
                    publish_selected_resource_claim(comms, store, admission, owner.name, resource)
            selected_owner = Comms(root).claim_projection()[str(resource)]
            assert selected_owner.admission == admission
            assert selected_owner.resource == str(resource)
            assert (
                publish_selected_resource_claim(comms, store, admission, owner.name, resource)
                == selected_owner
            )
            assert len(comms.full_history()) == 2
            write_selected_claimed_file(
                comms, store, admission, "Alice", selected_owner, b"value = 2\n"
            )
            assert resource.read_bytes() == b"value = 2\n"
            with pytest.raises(IdentityConflict):
                write_selected_claimed_file(
                    comms,
                    store,
                    admission,
                    "Alice",
                    replace(selected_owner, generation="0" * 32),
                    b"forged\n",
                )
            assert resource.read_bytes() == b"value = 2\n"
            foreign_root = Path(dirname) / "foreign"
            foreign_root.mkdir(mode=0o700)
            with MutationStore(str(foreign_root / "coordination.sqlite3")) as foreign:
                store._connection.backup(foreign._connection)
                with pytest.raises(IdentityConflict):
                    verify_selected_wake(comms, foreign, admission, owner.name)
            for candidate, name in (
                (replace(admission, recipient_lookup=bob_lookup), "Bob"),
                (replace(admission, wake_revision=engaged.revision + 1), "Alice"),
                (replace(admission, owner_admission_generation=admission_generation + 1), "Alice"),
                (replace(admission, turn_id="old-turn"), "Alice"),
            ):
                with pytest.raises(IdentityConflict):
                    verify_selected_wake(comms, store, candidate, name)
            fence = started.value.fence
            for phase in (AttemptPhase.PROMPT_ACCEPTED, AttemptPhase.MODEL_RUNNING):
                fence = store.advance_attempt(
                    fence, phase, expected_pointer_revision=started.value.snapshot.pointer_revision
                ).value.fence
            store.advance_attempt(
                fence,
                AttemptPhase.SETTLING,
                expected_pointer_revision=started.value.snapshot.pointer_revision,
                backend_done=True,
                process_dead=True,
            )
            with pytest.raises(IdentityConflict):
                verify_selected_wake(comms, store, admission, "Alice")
            with pytest.raises(IdentityConflict):
                write_selected_claimed_file(
                    comms, store, admission, "Alice", selected_owner, b"after settlement\n"
                )
            assert resource.read_bytes() == b"value = 2\n"
            with pytest.raises(IdentityConflict):
                publish_selected_resource_claim(
                    comms,
                    store,
                    replace(admission, operation_id="e" * 32),
                    "Alice",
                    resource,
                )
            comms.registry.unregister("Alice")
            with pytest.raises(IdentityConflict):
                verify_selected_wake(comms, store, admission, "Alice")
            with pytest.raises(IdentityConflict):
                publish_selected_resource_claim(comms, store, admission, "Alice", resource)
            assert len(comms.full_history()) == 2
