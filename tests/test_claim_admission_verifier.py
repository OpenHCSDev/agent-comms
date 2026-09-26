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
    observe_selected_resource_claim,
    publish_selected_resource_claim,
    verify_selected_wake,
)
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime import _engage
from agent_comms.coordination import AttemptPhase, ClaimDisposition
from agent_comms.coordination_cohort import accept_initial_cohort, sealed_cohort_claims
from agent_comms.coordination_store import IdentityConflict, MutationStore, prepare_fence_token
from agent_comms.declarations import ClaimEnvelopeUnknownError, Thread
from agent_comms.envelope_claim_transitions import WakeAdmission
from agent_comms.operations import Comms
from agent_comms.wake_injection import render_selected_wake_frame

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
            absent = observe_selected_resource_claim(comms, store, admission, owner.name, resource)
            assert not absent.observed and absent.claim_seq is None
            assert f"source #{message.seq}" in absent.message()
            no_wake = observe_selected_resource_claim(
                comms, store, replace(admission, recipient_lookup=bob_lookup), "Bob", resource
            )
            assert not no_wake.observed and no_wake.claim_seq is None
            wrong_source = observe_selected_resource_claim(
                comms, store, replace(admission, source_message_id="unrelated"), "Alice", resource
            )
            assert not wrong_source.observed and wrong_source.claim_seq is None
            assert resource.read_text() == "value = 1\n"
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
            observed = observe_selected_resource_claim(
                comms, store, admission, owner.name, resource
            )
            assert observed.observed and observed.claim_seq == selected_owner.seq
            assert observed.generation == selected_owner.generation
            assert observed.elapsed_ms >= observed.projection_ms >= 0
            assert observed.lock_wait_ms >= 0 and observed.lock_held_ms >= 0
            assert f"bus #{selected_owner.seq}" in observed.message()
            assert "not permission to write" in observed.message()
            frame = render_selected_wake_frame(
                initial,
                engaged,
                owner,
                phase="full",
                obligation=started.value.snapshot.obligation,
                claim_awareness=observed,
            )
            assert f"source #{message.seq}" in frame
            assert str(resource) in frame and "not file-write permission" in frame
            with pytest.raises(IdentityConflict):
                render_selected_wake_frame(
                    initial,
                    engaged,
                    owner,
                    phase="full",
                    obligation=started.value.snapshot.obligation,
                    claim_awareness=replace(observed, wake_claim_id="other"),
                )
            assert resource.read_text() == "value = 1\n"
            assert (
                publish_selected_resource_claim(comms, store, admission, owner.name, resource)
                == selected_owner
            )
            assert len(comms.full_history()) == 2
            other = worktree / "other.py"
            other.write_text("other = 1\n")
            comms.send_message("Bob", "#team", "Independent legacy claim", claims=["other.py"])
            conflict = observe_selected_resource_claim(comms, store, admission, "Alice", other)
            assert not conflict.observed and conflict.owner == "Alice"
            assert "Bob" in conflict.message() and "bus #" in conflict.message()
            assert "generation" in conflict.message() and other.read_text() == "other = 1\n"
            comms.send_message("Alice", "#team", "Release selected claim", releases=["module.py"])
            released = observe_selected_resource_claim(comms, store, admission, "Alice", resource)
            assert not released.observed and released.claim_seq is None
            comms.send_message("Bob", "#team", "Reclaim with new generation", claims=["module.py"])
            successor = observe_selected_resource_claim(comms, store, admission, "Alice", resource)
            assert not successor.observed and "Bob" in successor.message()
            assert successor.generation != selected_owner.generation
            assert resource.read_text() == "value = 1\n"
            with monkeypatch.context() as patch:
                clock = iter((0.0, 1.0))
                patch.setattr("agent_comms.claim_admission.time.monotonic", lambda: next(clock))
                overrun = observe_selected_resource_claim(
                    comms, store, admission, "Alice", resource, max_check_seconds=0.25
                )
            assert not overrun.observed and overrun.claim_seq is None
            assert overrun.elapsed_ms > 250 and "deadline" in overrun.message()
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
            settled = observe_selected_resource_claim(comms, store, admission, "Alice", resource)
            assert not settled.observed and settled.claim_seq is None
            drift = observe_selected_resource_claim(
                comms,
                store,
                replace(admission, owner_admission_generation=admission_generation + 1),
                "Alice",
                resource,
            )
            assert not drift.observed and drift.claim_seq is None
            with pytest.raises(IdentityConflict):
                publish_selected_resource_claim(
                    comms,
                    store,
                    replace(admission, operation_id="e" * 32),
                    "Alice",
                    resource,
                )
            comms.registry.unregister("Alice")
            assert not observe_selected_resource_claim(
                comms, store, admission, "Alice", resource
            ).observed
            with pytest.raises(IdentityConflict):
                verify_selected_wake(comms, store, admission, "Alice")
            with pytest.raises(IdentityConflict):
                publish_selected_resource_claim(comms, store, admission, "Alice", resource)
            assert len(comms.full_history()) == 5
            # Crash-partial append at the guarded private bus's pre-read lock
            # validation must deny, not leak an exception or resurrect an old
            # complete selected claim as a current ownership observation.
            with (root / "bus.jsonl").open("ab", buffering=0) as stream:
                stream.write(b'{"seq":999,')
                os.fsync(stream.fileno())
            torn = observe_selected_resource_claim(comms, store, admission, "Alice", resource)
            assert not torn.observed and torn.claim_seq is None and torn.generation is None
            assert f"source #{message.seq}" in torn.message()
            assert "guarded bus or registry unavailable" in torn.message()
            assert resource.read_text() == "value = 1\n"
