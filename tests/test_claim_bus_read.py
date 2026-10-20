"""A file claim in a selected turn reads the bus's messages, not its context rows.

Real private bus, checkpoint, registry and coordination stores under /var/tmp.
Context rows are nearly all of a long-lived bus (888 of 889 MB on the root
where this was found); reading them four times per claim took 3.5 minutes on
the owner's event loop with the wire, bus and registry locks held.
"""

from __future__ import annotations

from conftest import publish_initial
import hashlib
import os
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.attempt_start import AttemptStart
from agent_comms.child_process import ProcessIdentity
from agent_comms.selected_claim import (
    publish_selected_resource_claim,
    write_selected_claimed_file,
)
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import accept_delivery_cohort, sealed_cohort_assignments
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_tables.executions import ExecutionOrigin
from agent_comms.coordinator import Coordination
from agent_comms.envelope_claim_transitions import ExistingFileClaim, NativeClaim, WakeClaim
from agent_comms.coordination_session import NativeBoundary
from agent_comms.message_page import ConversationMessagePageRequest, MessagePageRequest
from agent_comms.channel_targets import CommandTarget
from agent_comms.messages import ClaimMessage, ClaimNoticeKind
from agent_comms.owner_fence import prepare_fence_token
from agent_comms.read_basis import AllChannelDisplayScope, DMDisplayScope
from agent_comms.selected_source_batch import SelectedSource
from agent_comms.thread_identity import TurnId, TurnIdentity
from agent_comms.threads import Thread
from agent_comms.turn_context import (
    ContextManifest,
    OwnerProvenance,
    RecordedContextTurn,
    SegmentManifest,
    TranscriptSegment,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="private cohort claim_scope requires real /var/tmp"
)


def record_context(comms: Comms, name: str, generation: int) -> None:
    owner = comms.registry.require(name)
    segment = SegmentManifest(
        TranscriptSegment,
        (OwnerProvenance(owner.incarnation, "input-source"),),
        hashlib.sha256(b"context").hexdigest(),
        7,
        2,
    )
    comms.contexts.record(ContextManifest(
        owner.incarnation,
        RecordedContextTurn(TurnId(f"turn-{generation}"), TurnIdentity(owner.incarnation, generation)),
        (segment,),
        "pi.estimateTokens",
    ))


@contextmanager
def selected_turn():
    """Alice runs a selected turn for one message; context rows surround it."""
    with TemporaryDirectory(prefix="ac-claim-read-") as dirname:
        root = Path(dirname) / "wire"
        root.mkdir(mode=0o700)
        worktree = Path(dirname) / "work"
        worktree.mkdir(mode=0o700)
        resource = worktree / "module.py"
        resource.write_text("value = 1\n")
        comms = Comms(root)
        for name, created in (("sender", 17021.0), ("Alice", 17022.0)):
            comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(worktree), process_identity=ProcessIdentity.capture(os.getpid()), created_at=created))
        root_id = comms.messaging.initialize_private_initial_protocol()
        record_context(comms, "Alice", 1)
        message = publish_initial(comms, "sender", "#team", "@Alice investigate")
        record_context(comms, "Alice", 2)
        initial = comms.bus.log.read_delivery_cohort(root_id, message.seq)
        with Coordination(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            for recipient in initial.audience.recipients:
                store.participants.register(
                    recipient.recipient_lookup, recipient.canonical_thread,
                    recipient.canonical_thread, committed=True,
                )
            accept_delivery_cohort(comms.bus, root_id, message.seq, store)
            lookup = RecordedThreadIncarnation("fixture", comms.registry.require("Alice").created_at).lookup
            assignment = sealed_cohort_assignments(store, lookup)[0]
            owner, generation = comms.registry.live_owner_with_claim("Alice")
            owner, generation = comms.registry.lease_live_turn_with_claim(
                owner, "selected-turn", expected_generation=generation
            )
            person = store.participants.get(lookup)
            store.executions.create(
                "execution", ExecutionOrigin.WIRE, lookup, owner.name, 1,
                sources=(SelectedSource(assignment.assignment_id, store.assignments, initial),),
            )
            snapshot = store.snapshots.get("execution")
            snapshot = store.executions.mark_pending(
                "execution", expected_revision=snapshot.execution.revision
            ).value
            store.attempts.start(AttemptStart(
                "execution", 1, owner.name, person.participant_generation, prepare_fence_token(),
                expected_execution_revision=snapshot.execution.revision,
                expected_pointer_revision=snapshot.pointer_revision,
            ))
            claim_scope = WakeClaim(
                wire_root_id=root_id,
                source_seq=message.seq,
                source_message_id=message.message_id,
                wake_assignment_id=assignment.assignment_id,
                wake_revision=store.assignments.get(assignment.assignment_id).revision,
                recipient_lookup=lookup,
                execution_id="execution",
                operation_id="f" * 32,
                owner_claimed_generation=generation,
                turn_id=owner.require_turn_lease().turn_id,
                participant_generation=person.participant_generation,
                attempt_ordinal=1,
            )
            yield comms, store, claim_scope, resource


def settle_failed(store):
    from agent_comms.attempt_states import AttemptFailedAttempt

    with store.session.settlement() as boundary:
        snapshot = store.snapshots.get("execution")
        snapshot.attempt.advance(
            store.session, type(snapshot.attempt.lifecycle), backend_done=True,
            process_dead=True, progress=False, reason_code=None,
        )
        store.snapshots.get("execution").settle(store.session, AttemptFailedAttempt(), None, boundary)


def claim_originals(comms: Comms) -> tuple[ClaimMessage, ...]:
    with comms.bus.log.full_history_snapshot() as (_, messages):
        return tuple(message for message in messages if isinstance(message, ClaimMessage))


def acquire_tool_claim(comms: Comms, name: str, resource: Path, operation_id: str = "e" * 32):
    with NativeBoundary(comms.bus) as boundary:
        owner = boundary.registry.require(name)
        scope = NativeClaim(
            owner_claimed_generation=boundary.registry.claim_generation(name),
            operation_id=operation_id, turn_id=f"turn-{name}", input_id=f"input-{name}",
        )
        return comms.bus.publisher._publish_acquisition_unlocked(
            boundary.bus_lock, boundary.registry, owner,
            ExistingFileClaim(resource).normalized(Path(owner.worktree)), scope,
            activity=boundary.activity,
        )


def release_tool_claim(comms: Comms, claim) -> None:
    with NativeBoundary(comms.bus) as boundary:
        comms.bus.publisher.release_resources(boundary, (claim,), "Finished test tool operation")


def test_claim_write_and_release_read_no_context_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    with selected_turn() as (comms, store, claim_scope, resource):
        log = comms.bus.log

        def whole_bus(*_, **__):
            raise AssertionError("a claim read every row of the bus")

        with monkeypatch.context() as patch:
            patch.setattr(log, "verified_records_unlocked", whole_bus)
            claimed = publish_selected_resource_claim(
                comms, store, claim_scope, "Alice", ExistingFileClaim(resource)
            )
            # Asking again for the same file in the same turn returns the held claim.
            assert publish_selected_resource_claim(
                comms, store, claim_scope, "Alice", ExistingFileClaim(resource)
            ) == claimed
            write_selected_claimed_file(comms, store, claim_scope, "Alice", claimed, b"value = 2\n")
            record_context(comms, "Alice", 3)
            settle_failed(store)
        assert resource.read_bytes() == b"value = 2\n"
        # The strict whole-bus reader agrees with what the claim path left.
        assert dict(log.claim_projection()) == {}
        claims = claim_originals(comms)
        displayed = tuple(message for message in comms.bus.log.read_page(
            MessagePageRequest(AllChannelDisplayScope("#any")),
        ).messages if isinstance(message, ClaimMessage))
        assert displayed == claims
        assert [(m.claim_transition.claims, len(m.claim_transition.releases)) for m in claims] == [
            ((str(resource),), 0), ((), 1),
        ]
        assert all(message.type is ClaimNoticeKind for message in claims)
        assert (claims[0].sender, claims[0].target, claims[0].body) == (
            "Alice", "Alice", "Claimed",
        )
        snapshot = comms.registry.snapshot()
        dm_scope = DMDisplayScope.capture("Alice", "sender", snapshot)
        assert comms.bus.log.read_page(ConversationMessagePageRequest(dm_scope)).messages == ()
        raw_channel = comms.views.history_page(CommandTarget.named("#team")).messages
        assert [message.body for message in raw_channel] == ["@Alice investigate"]
        channel = comms.views.channel_display_page("#team").messages
        assert [message.body for message in channel] == ["@Alice investigate"]
        assert all(message.role.notification_references(message) for message in channel)
        with pytest.raises(ValueError, match="not generically sendable"):
            comms.messaging.send(
                "Alice", "sender", "Invented claim notice", ClaimNoticeKind
            ).message


def test_claim_operation_is_refused_after_its_claim_was_released() -> None:
    with selected_turn() as (comms, store, claim_scope, resource):
        claimed = publish_selected_resource_claim(
            comms, store, claim_scope, "Alice", ExistingFileClaim(resource)
        )
        assert dict(comms.bus.log.claim_projection()) == {str(resource): claimed}
        release_tool_claim(comms, claimed)
        with pytest.raises(IdentityConflict, match="already consumed"):
            publish_selected_resource_claim(
                comms, store, claim_scope, "Alice", ExistingFileClaim(resource)
            )
        again = publish_selected_resource_claim(
            comms, store, replace(claim_scope, operation_id="e" * 32), "Alice",
            ExistingFileClaim(resource),
        )
        assert again.generation != claimed.generation
        assert dict(comms.bus.log.claim_projection()) == {str(resource): again}


