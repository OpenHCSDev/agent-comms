"""Following proof uses real session/journal/input rows, without a native/provider run.

These controls exercise the shared event verifier and durable transitions. The
actual TrackedTurnSession event-loop/transport qualification remains root-owned.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field, replace
from pathlib import Path
import sqlite3

import pytest

from agent_comms.turn_inbox import InputContent, InputRequest, SteerInputPolicy
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import InputId
from agent_comms.lifecycle_capability import Unresolved
from agent_comms.input_disposition import InputDispositions
from agent_comms.input_key import InputKey
from agent_comms.input_origin import WireInputOrigin
from agent_comms.message_reference import MessageReference
from agent_comms.native_failures import NativePiUnavailable
from agent_comms.native_input_proof import NativeContextJournal, NativeContextProof
from agent_comms.native_tool_call import OwnerToolConnection
from agent_comms.notification_progress import DeliveryProgress, ShownToModelProgress
from agent_comms.pi_commands import FollowingPrompt
from agent_comms.backend import ObservedNativeCommit, TrackedInputContext
from agent_comms.turn_item import FreshNativeId
from agent_comms.turn_lease import TurnLeaseFence
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.pi_events import ContextCommitted, InputCommitted
from agent_comms.private_send_claim import PrivateSendClaim
from agent_comms.notification_evidence import AllOutcomes
from agent_comms.private_sidecar import native_request_digest
from conftest import accept_initial, publish_initial
from native_proof_cases import append_input_entry
from test_coordinated_runtime import tmp_path  # noqa: F401
from test_live_context_notifications import acknowledge_context, recorded_input_context
from test_native_failure_recovery import prepared_native_input


@asynccontextmanager
async def following_session(directory: Path) -> AsyncIterator[PrivateSendClaim]:
    async with prepared_native_input(directory) as claim:
        await acknowledge_context(claim, await recorded_input_context(claim))
        yield claim


@dataclass(frozen=True)
class FollowingContext:
    claim: PrivateSendClaim
    keys: tuple[InputKey, ...]
    references: tuple[MessageReference, ...]
    text: str
    input_event: InputCommitted
    context_event: ContextCommitted
    public_id: InputId = field(default_factory=InputId.mint)

    @property
    def dispositions(self) -> InputDispositions:
        return InputDispositions(self.claim.selected.comms.root)

    @property
    def lease(self) -> TurnLeaseFence:
        return self.claim.owner.thread.require_turn_lease()

    def notification_states(self) -> tuple[DeliveryProgress, ...]:
        """The projected state of each exact original for its one frozen recipient."""
        return notification_states(self.claim, self.references)

    def start(self) -> None:
        for key in self.keys:
            assert self.dispositions.started(
                key, turn_id=self.lease.turn_id, native_id=self.input_event.input_id,
                text=self.text,
            )

    @property
    def command(self) -> FollowingPrompt:
        return FollowingPrompt(
            input=InputRequest(input_id=self.public_id, content=InputContent(text=self.text),
                               when_busy=SteerInputPolicy),
            input_id=self.input_event.input_id, message=self.text,
            streaming_behavior=SteerInputPolicy,
            # This command-data control never starts a tool transport or connects here.
            owner_tools=OwnerToolConnection(str(self.claim.session.identity.path) + '.unused', 'unused'),
        )

    async def verify(self, event: ContextCommitted | None = None) -> NativeContextProof:
        observed = TrackedInputContext(
            self.command, ObservedNativeCommit(self.input_event),
            ObservedNativeCommit(event or self.context_event),
        )
        async with self.claim.selected.cursor_delivery.reading() as reads:
            return observed.proof(
                self.claim.session.files(self.claim.selected.comms.root),
                self.input_event.session_id, reads,
            )

    def acknowledge(self, context: NativeContextProof) -> None:
        with self.claim.selected.comms.registry.store.current_snapshot() as cut:
            self.dispositions.context_committed(
                self.keys, lease=self.lease, sent_text=self.text,
                context=context, snapshot=cut.snapshot,
            )

    def append_generation(self, generation: int) -> FollowingContext:
        files = self.claim.session.files(self.claim.selected.comms.root)
        with closing(sqlite3.connect(files.input_proof)) as db, db:
            original = NativeContextJournal.for_input(
                db, self.input_event.input_id,
                range(self.context_event.request_generation, self.context_event.request_generation + 1),
            )
            later = replace(original, request_generation=generation,
                            committed_at_ms=original.committed_at_ms + 1,
                            llm_context_digest='b' * 64)
            later.insert(db)
        return replace(self, context_event=replace(
            self.context_event, request_generation=later.request_generation,
            llm_context_digest=later.llm_context_digest, committed_at_ms=later.committed_at_ms,
        ))


def notification_states(
    claim: PrivateSendClaim, references: tuple[MessageReference, ...],
) -> tuple[DeliveryProgress, ...]:
    window = claim.selected.comms.views.message_notifications_for_references(AllOutcomes(), references)
    owner = claim.owner.thread.incarnation.require_recorded().lookup
    rows = tuple(row for reference in references
                 for row in window.outcomes[(reference.seq, reference.message_id)])
    assert [row.recipient_identity.recipient_lookup for row in rows] == [owner] * len(references)
    return tuple(row.state for row in rows)


def following_input(claim: PrivateSendClaim, *, generation: int, count: int = 1) -> FollowingContext:
    """Reserve and bind originals through InputDispositions, then retain native evidence.

    No message/start/context event is inferred from another input or a reply.
    """
    comms, owner = claim.selected.comms, claim.owner.thread
    lease = owner.require_turn_lease()
    messages = tuple(publish_initial(comms, 'sender', owner.name,
                                     f'Following original {generation}/{index}')
                     for index in range(count))
    with Coordination(claim.store_path) as store:
        for message in messages:
            assert accept_initial(comms.bus, claim.selected.root_id, message.seq, store)
    keys = tuple(InputDispositions.bus_key(message.message, owner) for message in messages)
    text = '\n'.join(message.message.body for message in messages)
    dispositions = InputDispositions(comms.root)
    for key, message in zip(keys, messages, strict=True):
        assert dispositions.record(
            key, seq=message.seq, owner=owner.name, claim_scope=lease.claimed_generation,
            target=owner.name, text=message.message.body,
            origin=WireInputOrigin(claim.selected.root_id, message.message.reference),
        )
    native_id = InputId.mint()
    assert dispositions.bind_originals(
        keys, claim_scope=lease.claimed_generation, turn_id=lease.turn_id,
        native_id=native_id, text=text,
    )
    session = claim.session.identity
    entry = FreshNativeId(f'following-{native_id}')
    start = append_input_entry(session.path, {
        'type': 'message', 'id': entry, 'timestamp': '2026-10-01T00:00:00.002Z',
        'message': {'role': 'user', 'inputId': native_id,
                    'inputDigest': native_request_digest(text, streaming_behavior=SteerInputPolicy),
                    'content': [{'type': 'text', 'text': text}]},
    })
    recorded = NativeContextJournal(
        input_id=native_id, session_id=session.session_id, session_entry_id=entry,
        request_generation=generation, llm_context_digest='a' * 64,
        committed_at_ms=1_791_964_800_001 + generation,
        schema=2, type='context_committed',
    )
    with closing(sqlite3.connect(claim.session.files(comms.root).input_proof)) as db, db:
        recorded.insert(db)
    return FollowingContext(
        claim, keys, tuple(message.message.reference for message in messages), text,
        InputCommitted(input_id=native_id, session_id=session.session_id,
                       session_entry_id=entry, session_entry_start=start),
        ContextCommitted(input_id=native_id, session_id=session.session_id,
                         session_entry_id=entry, request_generation=generation,
                         llm_context_digest=recorded.llm_context_digest,
                         committed_at_ms=recorded.committed_at_ms),
    )


async def test_two_following_ids_and_grouped_keys_keep_their_own_first_proof(tmp_path: Path):
    async with following_session(tmp_path) as claim:
        first = following_input(claim, generation=2, count=2)
        second = following_input(claim, generation=3)
        first.start()
        second.start()
        assert first.input_event.input_id != second.input_event.input_id != claim.input_id
        originals = tuple(row.source for row in claim.stage.assignments)
        assert all(state.consumed for state in notification_states(claim, originals))
        assert all(not row.model_contexts() for row in first.dispositions.read(
            first.keys + second.keys).rows.values())
        assert not any(state.consumed for state in first.notification_states() + second.notification_states())
        proof = await first.verify()
        first.acknowledge(proof)
        for row in first.dispositions.read(first.keys).rows.values():
            assert row.has_started and not row.items_with(Unresolved, row)
            assert row.model_contexts() == (proof,)
        assert first.notification_states() == (ShownToModelProgress(proof.committed_at_ms),) * 2
        assert all(not row.model_contexts() for row in second.dispositions.read(second.keys).rows.values())
        assert not any(state.consumed for state in second.notification_states())
        second_proof = await second.verify()
        second.acknowledge(second_proof)
        assert all(row.model_contexts() == (second_proof,)
                   for row in second.dispositions.read(second.keys).rows.values())
        assert second.notification_states() == (ShownToModelProgress(second_proof.committed_at_ms),)
        assert first.notification_states() == (ShownToModelProgress(proof.committed_at_ms),) * 2
        with Coordination(claim.store_path) as store:
            original = NativeRuntimeInput.require(store.session._connection, input_id=claim.input_id)
            assert original.require_context_proof().input_id == claim.input_id
            assert len(NativeRuntimeInput.select(store.session._connection)) == 1
        assert claim.stage.progress.current.starting  # No terminal answer was supplied.


async def test_one_emitted_event_alone_cannot_prove_context(tmp_path: Path):
    async with following_session(tmp_path) as claim:
        case = following_input(claim, generation=2)
        case.start()
        for incomplete in (
            TrackedInputContext(case.command, input_commit=ObservedNativeCommit(case.input_event)),
            TrackedInputContext(case.command, context_commit=ObservedNativeCommit(case.context_event)),
        ):
            async with claim.selected.cursor_delivery.reading() as reads:
                with pytest.raises(NativePiUnavailable):
                    incomplete.proof(claim.session.files(claim.selected.comms.root),
                                     case.input_event.session_id, reads)
        assert all(not row.model_contexts() for row in case.dispositions.read(case.keys).rows.values())


async def test_foreign_journal_event_and_mixed_key_batch_refuse_without_partial_write(tmp_path: Path):
    async with following_session(tmp_path) as claim:
        first = following_input(claim, generation=2)
        second = following_input(claim, generation=3)
        first.start()
        second.start()
        with pytest.raises(NativePiUnavailable):
            await first.verify(second.context_event)
        with pytest.raises(NativePiUnavailable):
            await first.verify(replace(first.context_event, llm_context_digest='c' * 64))
        proof = await first.verify()
        with claim.selected.comms.registry.store.current_snapshot() as cut:
            with pytest.raises(RelationViolationError):
                first.dispositions.context_committed(
                    first.keys + second.keys, lease=first.lease, sent_text=first.text,
                    context=proof, snapshot=cut.snapshot,
                )
        assert all(not row.model_contexts() for row in first.dispositions.read(
            first.keys + second.keys).rows.values())


async def test_duplicate_and_later_generation_keep_first_proof_and_no_replay(tmp_path: Path):
    async with following_session(tmp_path) as claim:
        case = following_input(claim, generation=2)
        case.start()
        proof = await case.verify()
        case.acknowledge(proof)
        case.acknowledge(proof)
        later = case.append_generation(3)
        later.acknowledge(await later.verify())
        assert all(row.model_contexts() == (proof,) for row in case.dispositions.read(case.keys).rows.values())
        with pytest.raises(IdentityConflict):
            case.acknowledge(replace(proof, llm_context_digest='c' * 64))
        assert not case.dispositions.bind_originals(
            case.keys, claim_scope=case.lease.claimed_generation, turn_id=case.lease.turn_id,
            native_id=InputId.mint(), text=case.text,
        )


async def test_cancelled_input_or_released_lease_cannot_acquire_context(tmp_path: Path):
    async with following_session(tmp_path) as claim:
        cleared = following_input(claim, generation=2)
        for key in cleared.keys:
            cleared.dispositions.clear_native(key, cleared.input_event.input_id, cleared.text)
        with pytest.raises(RelationViolationError):
            cleared.acknowledge(await cleared.verify())
        live = following_input(claim, generation=3)
        live.start()
        proof = await live.verify()
        lease = live.lease
        assert claim.selected.comms.registry.release_turn(lease)[0]
        with pytest.raises(RelationViolationError):
            live.acknowledge(proof)
        assert all(not row.model_contexts() for row in live.dispositions.read(live.keys).rows.values())
