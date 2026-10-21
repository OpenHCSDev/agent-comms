"""Sealed N/K claim_scope checks against real private bus, registry, and SQLite stores."""

from __future__ import annotations

from conftest import publish_initial
import os
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import agent_comms.selected_claim as selected_claim
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.assignment_states import FullPendingAssignment
from agent_comms.attempt_start import AttemptStart
from agent_comms.attempt_states import ModelRunningAttempt, PromptAcceptedAttempt, SettlingAttempt
from agent_comms.child_process import ProcessIdentity
from agent_comms.selected_claim import (
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
from agent_comms.envelope_claim_transitions import ExistingFileClaim, WakeClaim
from agent_comms.errors import ClaimEnvelopeUnknownError, RelationViolationError
from agent_comms.owner_fence import prepare_fence_token
from agent_comms.message_page import MessagePageRequest
from agent_comms.read_basis import AllChannelDisplayScope
from agent_comms.selected_source_batch import SelectedSource

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="private cohort claim_scope requires real /var/tmp"
)


def test_selected_wake_verifier_refuses_no_wake_and_stale_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory(prefix="ac-claim-verify-") as dirname:
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
                comms.registry.new_thread(name, frozenset({'team'}), str(worktree), process_identity=ProcessIdentity.capture(os.getpid()), created_at=created)
            )
        root_id = comms.messaging.initialize_private_initial_protocol()
        message = publish_initial(comms, "sender", "#team", "@Alice investigate")
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
            alice_lookup = RecordedThreadIncarnation("fixture", comms.registry.require("Alice").created_at).lookup
            bob_lookup = RecordedThreadIncarnation("fixture", comms.registry.require("Bob").created_at).lookup
            assert sealed_cohort_assignments(store, bob_lookup) == ()
            assignment = sealed_cohort_assignments(store, alice_lookup)[0]
            assert type(assignment.lifecycle) is FullPendingAssignment
            owner, claimed_generation = comms.registry.live_owner_with_claim("Alice")
            owner, claimed_generation = comms.registry.lease_live_turn_with_claim(
                owner, "selected-turn", expected_generation=claimed_generation
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
                person.owner_identity.generation,
                prepare_fence_token(),
                expected_execution_revision=snapshot.execution.revision,
                expected_pointer_revision=snapshot.pointer_revision,
            ))
            engaged = store.assignments.get(assignment.assignment_id)
            claim_scope = WakeClaim(
                wire_root_id=root_id,
                source_seq=message.seq,
                source_message_id=message.message_id,
                wake_assignment_id=assignment.assignment_id,
                wake_revision=engaged.revision,
                recipient_lookup=alice_lookup,
                execution_id=execution_id,
                operation_id="f" * 32,
                owner_claimed_generation=claimed_generation,
                turn_id=owner.require_turn_lease().turn_id,
                participant_generation=person.owner_identity.generation,
                attempt_ordinal=1,
            )
            verify_selected_wake(comms, store, claim_scope, owner.name)
            with pytest.raises(IdentityConflict):
                publish_selected_resource_claim(
                    comms,
                    store,
                    replace(claim_scope, recipient_lookup=bob_lookup),
                    "Bob",
                    ExistingFileClaim(Path(resource)),
                )
            assert comms.bus.log.claim_projection().get(str(resource)) is None
            append = comms.bus.log.append_private_unlocked

            def append_then_lose_receipt(metadata: dict[str, int | str], row: dict) -> None:
                append(metadata, row)
                raise OSError("lost acknowledgement after durable append")

            with monkeypatch.context() as patch:
                patch.setattr(comms.bus.log, "append_private_unlocked", append_then_lose_receipt)
                with pytest.raises(ClaimEnvelopeUnknownError):
                    publish_selected_resource_claim(
                        comms, store, claim_scope, owner.name, ExistingFileClaim(Path(resource))
                    )
            selected_owner = Comms(root).bus.log.claim_projection()[str(resource)]
            assert selected_owner.claim_scope == claim_scope
            assert selected_owner.resource == str(resource)
            assert (
                publish_selected_resource_claim(
                    comms, store, claim_scope, owner.name, ExistingFileClaim(Path(resource))
                )
                == selected_owner
            )
            assert len(comms.bus.log.read_page(
                MessagePageRequest(AllChannelDisplayScope("#any")),
            ).messages) == 2
            # Simulate a crash after the durable rename intent but before SQL
            # owner CAS: an already selected attempt and exact resource claim
            # must not admit a new claim or touch the existing file.
            intent = root / ".private-owner-rename.pending"
            intent.write_text("pending owner transition")
            with intent.open("rb") as stream:
                os.fsync(stream.fileno())
            with pytest.raises(RelationViolationError):
                verify_selected_wake(comms, store, claim_scope, "Alice")
            with pytest.raises(RelationViolationError):
                publish_selected_resource_claim(
                    comms, store, claim_scope, "Alice", ExistingFileClaim(Path(resource))
                )
            with pytest.raises(RelationViolationError):
                write_selected_claimed_file(
                    comms, store, claim_scope, "Alice", selected_owner, b"forbidden\n"
                )
            assert resource.read_bytes() == b"value = 1\n"
            intent.unlink()  # Fixture-only return to the positive pre-rename case.
            outside = Path(dirname) / "outside"
            outside.mkdir(mode=0o700)
            external = outside / "module.py"
            external.write_bytes(b"outside untouched\n")
            displaced = worktree / "displaced-pkg"
            from agent_comms.private_bus_checkpoint import CheckedRead

            actual_claim = CheckedRead.claim

            def swap_after_claim(source, resource):
                current = actual_claim(source, resource)
                parent.rename(displaced)
                parent.symlink_to(outside, target_is_directory=True)
                return current

            # Swap after canonicalization and the indexed current-claim read:
            # old absolute stat/open followed the parent symlink despite
            # final-component O_NOFOLLOW.
            with monkeypatch.context() as patch:
                patch.setattr(CheckedRead, "claim", swap_after_claim)
                with pytest.raises(IdentityConflict):
                    write_selected_claimed_file(
                        comms, store, claim_scope, "Alice", selected_owner, b"escaped\n"
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
                patch.setattr(selected_claim.os, "open", swap_after_open)
                with pytest.raises(IdentityConflict):
                    write_selected_claimed_file(
                        comms, store, claim_scope, "Alice", selected_owner, b"late escape\n"
                    )
            assert external.read_bytes() == b"outside untouched\n"
            assert (displaced / "module.py").read_bytes() == b"value = 1\n"
            parent.unlink()
            displaced.rename(parent)
            write_selected_claimed_file(
                comms, store, claim_scope, "Alice", selected_owner, b"value = 2\n"
            )
            assert resource.read_bytes() == b"value = 2\n"
            with pytest.raises(IdentityConflict):
                write_selected_claimed_file(
                    comms,
                    store,
                    claim_scope,
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
                    verify_selected_wake(comms, foreign, claim_scope, owner.name)
            for candidate, name in (
                (replace(claim_scope, recipient_lookup=bob_lookup), "Bob"),
                (replace(claim_scope, wake_revision=engaged.revision + 1), "Alice"),
                (replace(claim_scope, owner_claimed_generation=claimed_generation + 1), "Alice"),
                (replace(claim_scope, turn_id="old-turn"), "Alice"),
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
                verify_selected_wake(comms, store, claim_scope, "Alice")
            with pytest.raises(IdentityConflict):
                write_selected_claimed_file(
                    comms, store, claim_scope, "Alice", selected_owner, b"after settlement\n"
                )
            assert resource.read_bytes() == b"value = 2\n"
            with pytest.raises(IdentityConflict):
                publish_selected_resource_claim(
                    comms,
                    store,
                    replace(claim_scope, operation_id="e" * 32),
                    "Alice",
                    ExistingFileClaim(Path(resource)),
                )
            comms.registry.unregister("Alice")
            with pytest.raises(IdentityConflict):
                verify_selected_wake(comms, store, claim_scope, "Alice")
            with pytest.raises(IdentityConflict):
                publish_selected_resource_claim(
                    comms, store, claim_scope, "Alice", ExistingFileClaim(Path(resource))
                )
            assert len(comms.bus.log.read_page(
                MessagePageRequest(AllChannelDisplayScope("#any")),
            ).messages) == 2
