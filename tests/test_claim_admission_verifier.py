"""Sealed N/K admission checks against real private bus, registry, and SQLite stores."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms import claim_admission
from agent_comms.assignment_states import FullPendingAssignment
from agent_comms.attempt_start import AttemptStart
from agent_comms.attempt_states import ModelRunningAttempt, PromptAcceptedAttempt, SettlingAttempt
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.claim_admission import (
    publish_selected_resource_claim,
    verify_selected_wake,
    write_selected_claimed_file,
)
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import accept_delivery_cohort, sealed_cohort_assignments
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_tables.executions import ExecutionOrigin
from agent_comms.coordinator import Coordination
from agent_comms.envelope_claim_transitions import ExistingFileClaim, WakeAdmission
from agent_comms.errors import ClaimEnvelopeUnknownError, RelationViolationError
from agent_comms.owner_fence import prepare_fence_token
from agent_comms.selected_source_batch import SelectedSource
from agent_comms.threads import Thread

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
        parent = worktree / "pkg"
        parent.mkdir(mode=0o700)
        resource = parent / "module.py"
        resource.write_text("value = 1\n")
        comms = Comms(root)
        for name, created in (("sender", 17021.0), ("Alice", 17022.0), ("Bob", 17023.0)):
            comms.registry.declare(
                Thread(
                    name,
                    frozenset({"team"}),
                    str(worktree),
                    process_identity=ProcessIdentity.capture(os.getpid()),
                    created_at=created,
                )
            )
        root_id = comms.messaging.initialize_private_initial_protocol()
        message = comms.messaging.send_initial_cohort("sender", "#team", "@Alice investigate")
        initial = comms.bus.log.read_delivery_cohort(root_id, message.seq)
        with Coordination(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            for recipient in initial.audience.recipients:
                store.participants.register(
                    recipient.recipient_lookup,
                    recipient.canonical_thread,
                    recipient.canonical_thread,
                    committed=True,
                )
            accept_delivery_cohort(comms.bus, root_id, message.seq, store)
            alice_lookup = stable_thread_lookup(comms.registry.require("Alice").created_at)
            bob_lookup = stable_thread_lookup(comms.registry.require("Bob").created_at)
            assert sealed_cohort_assignments(store, bob_lookup) == ()
            assignment = sealed_cohort_assignments(store, alice_lookup)[0]
            assert type(assignment.lifecycle) is FullPendingAssignment
            owner, admission_generation = comms.registry.live_owner_with_admission("Alice")
            owner, admission_generation = comms.registry.lease_live_turn_with_admission(
                owner, "selected-turn", expected_generation=admission_generation
            )
            person = store.participants.get(alice_lookup)
            execution_id = "verifier-execution"
            store.executions.create(
                execution_id,
                ExecutionOrigin.WIRE,
                alice_lookup,
                owner.name,
                1,
                sources=(SelectedSource(assignment.assignment_id, store.assignments, initial),),
            )
            snapshot = store.snapshots.get(execution_id)
            snapshot = store.executions.mark_pending(
                execution_id, expected_revision=snapshot.execution.revision
            ).value
            started = store.attempts.start(AttemptStart(
                execution_id,
                1,
                owner.name,
                person.participant_generation,
                prepare_fence_token(),
                expected_execution_revision=snapshot.execution.revision,
                expected_pointer_revision=snapshot.pointer_revision,
            ))
            engaged = store.assignments.get(assignment.assignment_id)
            admission = WakeAdmission(
                wire_root_id=root_id,
                source_seq=message.seq,
                source_message_id=message.message_id,
                wake_assignment_id=assignment.assignment_id,
                wake_revision=engaged.revision,
                recipient_lookup=alice_lookup,
                execution_id=execution_id,
                operation_id="f" * 32,
                owner_admission_generation=admission_generation,
                turn_id=owner.active_turn.id,
                participant_generation=person.participant_generation,
                attempt_ordinal=1,
            )
            verify_selected_wake(comms, store, admission, owner.name)
            with pytest.raises(IdentityConflict):
                publish_selected_resource_claim(
                    comms,
                    store,
                    replace(admission, recipient_lookup=bob_lookup),
                    "Bob",
                    ExistingFileClaim(Path(resource)),
                )
            assert comms.bus.log.claim_projection().get(str(resource)) is None
            append = comms.bus.log._append_private_unlocked

            def append_then_lose_receipt(metadata: dict[str, int | str], row: dict) -> None:
                append(metadata, row)
                raise OSError("lost acknowledgement after durable append")

            with monkeypatch.context() as patch:
                patch.setattr(comms.bus.log, "_append_private_unlocked", append_then_lose_receipt)
                with pytest.raises(ClaimEnvelopeUnknownError):
                    publish_selected_resource_claim(
                        comms, store, admission, owner.name, ExistingFileClaim(Path(resource))
                    )
            selected_owner = Comms(root).bus.log.claim_projection()[str(resource)]
            assert selected_owner.admission == admission
            assert selected_owner.resource == str(resource)
            assert (
                publish_selected_resource_claim(
                    comms, store, admission, owner.name, ExistingFileClaim(Path(resource))
                )
                == selected_owner
            )
            assert len(comms.views.full_history()) == 2
            # Simulate a crash after the durable rename intent but before SQL
            # owner CAS: an already selected attempt and exact resource claim
            # must not admit a new claim or touch the existing file.
            intent = root / ".private-owner-rename.pending"
            intent.write_text("pending owner transition")
            with intent.open("rb") as stream:
                os.fsync(stream.fileno())
            with pytest.raises(RelationViolationError, match="Private owner rename is pending"):
                verify_selected_wake(comms, store, admission, "Alice")
            with pytest.raises(RelationViolationError, match="Private owner rename is pending"):
                publish_selected_resource_claim(
                    comms, store, admission, "Alice", ExistingFileClaim(Path(resource))
                )
            with pytest.raises(RelationViolationError, match="Private owner rename is pending"):
                write_selected_claimed_file(
                    comms, store, admission, "Alice", selected_owner, b"forbidden\n"
                )
            assert resource.read_bytes() == b"value = 1\n"
            intent.unlink()  # Fixture-only return to the positive pre-rename case.
            outside = Path(dirname) / "outside"
            outside.mkdir(mode=0o700)
            external = outside / "module.py"
            external.write_bytes(b"outside untouched\n")
            displaced = worktree / "displaced-pkg"
            actual_projection = comms.bus.log._claim_projection_unlocked

            def swap_after_projection(marker):
                projection = actual_projection(marker)
                parent.rename(displaced)
                parent.symlink_to(outside, target_is_directory=True)
                return projection

            # Swap *after* canonicalization and durable claim projection:
            # old absolute stat/open followed the parent symlink despite
            # final-component O_NOFOLLOW.
            with monkeypatch.context() as patch:
                patch.setattr(comms.bus.log, "_claim_projection_unlocked", swap_after_projection)
                with pytest.raises(IdentityConflict, match="directory or file changed"):
                    write_selected_claimed_file(
                        comms, store, admission, "Alice", selected_owner, b"escaped\n"
                    )
            assert external.read_bytes() == b"outside untouched\n"
            assert (displaced / "module.py").read_bytes() == b"value = 1\n"
            parent.unlink()
            displaced.rename(parent)

            actual_open = os.open

            def swap_after_open(path, flags, *args, **kwargs):
                fd = actual_open(path, flags, *args, **kwargs)
                if path == "module.py" and kwargs.get("dir_fd") is not None:
                    parent.rename(displaced)
                    parent.symlink_to(outside, target_is_directory=True)
                return fd

            # A parent rename after obtaining the leaf fd but before truncate
            # must be detected against the pinned chain and must not mutate.
            with monkeypatch.context() as patch:
                patch.setattr(claim_admission.os, "open", swap_after_open)
                with pytest.raises(IdentityConflict, match="parent directory changed"):
                    write_selected_claimed_file(
                        comms, store, admission, "Alice", selected_owner, b"late escape\n"
                    )
            assert external.read_bytes() == b"outside untouched\n"
            assert (displaced / "module.py").read_bytes() == b"value = 1\n"
            parent.unlink()
            displaced.rename(parent)
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
            with Coordination(str(foreign_root / "coordination.sqlite3")) as foreign:
                store.session._connection.backup(foreign.session._connection)
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
            for phase in (PromptAcceptedAttempt, ModelRunningAttempt):
                fence = store.attempts.advance(
                    fence, phase, expected_pointer_revision=started.value.snapshot.pointer_revision
                ).value.fence
            store.attempts.advance(
                fence,
                SettlingAttempt,
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
                    ExistingFileClaim(Path(resource)),
                )
            comms.registry.unregister("Alice")
            with pytest.raises(IdentityConflict):
                verify_selected_wake(comms, store, admission, "Alice")
            with pytest.raises(IdentityConflict):
                publish_selected_resource_claim(
                    comms, store, admission, "Alice", ExistingFileClaim(Path(resource))
                )
            assert len(comms.views.full_history()) == 2
