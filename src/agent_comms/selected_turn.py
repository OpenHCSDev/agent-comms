"""Selected triage and full attempts consume actual reservation and lifecycle owners."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from .attempt_start import AttemptStart
from .assignment_states import CompletedAssignment
from .channel_coding_tools import CodingToolOwner
from .coordination_errors import IdentityConflict, StaleFence
from .coordination_tables.executions import ExecutionOrigin
from .coordination_snapshot import RecoverySnapshot
from .coordination_tables.responses import ResponseObligation
from .durable_turn import DurableTurn
from .envelope_claim_transitions import WakeAdmission
from .native_pi import NativePiUnavailable
from .optional_awareness_projection import OptionalAwarenessProjection
from .owner_fence import prepare_fence_token
from .private_send_stage import FullNativeSend, TriageNativeSend
from .selected_actions import BatchSelectedAction, SelectedAction
from .selected_participant import SelectedParticipant
from .selected_request import SelectedRequest
from .selected_result import CoordinatedTurn
from .selected_session import SelectedSession
from .selected_triage import SelectedTriageOutcome
from .wake_candidate_index import WakeCandidateIndex
from .context_segments.wake import SelectedWakeSegment
from .context_segments.selected import SelectedTriageSegment, SelectedWorkSegment
from .thread_identity import TurnId
from .turn_context import RecordedContextTurn, RenderedInput, TurnContext
from .turn_phase import PreparingPhase, PromptAcceptancePhase, PublishingPhase


@dataclass(frozen=True)
class SelectedPrompt:
    participant: SelectedParticipant

    def context(self, segments) -> TurnContext:
        owner = self.participant.owner.thread
        lease = owner.require_turn_lease()
        return TurnContext(
            owner.incarnation,
            RecordedContextTurn(TurnId(lease.turn_id), lease.identity),
            tuple(segments),
        )

    def triage(self) -> RenderedInput:
        participant = self.participant
        frame = SelectedWakeSegment.capture(
            tuple((source.delivery, source.assignment) for source in participant.batch.sources),
            participant.owner.thread,
        )
        return self.context(
            (frame, SelectedTriageSegment.capture(participant, frame.provenance))
        ).render()

    async def full(self, assignments, obligations, action: SelectedAction) -> RenderedInput:
        participant = self.participant
        frame = SelectedWakeSegment.capture(
            tuple(
                (source.delivery, assignment)
                for source, assignment in zip(participant.batch.sources, assignments, strict=True)
            ),
            participant.owner.thread,
            obligations=obligations,
        )
        projection = OptionalAwarenessProjection.for_selected(
            WakeCandidateIndex(participant.bus),
            through_seq=participant.batch.sources[-1].delivery.message.seq,
            generation=participant.identity.generation,
            admission_generation=participant.owner.admission_generation,
        )
        awareness = await projection.segments(
            participant.batch.sources[-1].delivery,
            assignments[-1],
            participant.owner.thread,
        )
        return self.context(
            (
                frame,
                *awareness,
                SelectedWorkSegment.capture(participant, action, frame.provenance),
                participant.batch.response_segment(participant.owner.thread.name),
            )
        ).render()


@dataclass(frozen=True)
class SelectedAttempt:
    """An engaged attempt always has its full native stage and evolving fence."""

    participant: SelectedParticipant
    stage: FullNativeSend
    token: str
    obligations: tuple[ResponseObligation, ...]

    @classmethod
    def engage(cls, participant: SelectedParticipant,
               snapshot: RecoverySnapshot) -> SelectedAttempt:
        participant.require_current(participant.store)
        store = participant.store
        assignment_ids = participant.batch.assignment_ids
        execution_id = participant.batch.execution_id
        if snapshot.execution.execution_id != execution_id:
            raise IdentityConflict("selected attempt differs from its created batch execution")
        if snapshot.execution.lifecycle.queued:
            snapshot = store.executions.mark_pending(
                execution_id,
                expected_revision=snapshot.execution.revision,
            ).value
        token = prepare_fence_token()
        started = store.attempts.start(
            AttemptStart(
                execution_id,
                1,
                participant.owner.thread.name,
                participant.identity.generation,
                token,
                expected_execution_revision=snapshot.execution.revision,
                expected_pointer_revision=snapshot.pointer_revision,
            )
        ).value
        progress = DurableTurn(store.attempts, started.fence, started.snapshot.pointer_revision, "")
        selected = started.snapshot.assignments
        if tuple(row.assignment_id for row in selected) != assignment_ids:
            raise IdentityConflict("full wake lost an original batch assignment")
        participant.transition(
            PreparingPhase(f"Preparing response to {len(selected)} messages in {', '.join(participant.batch.targets)}")
        )
        return cls(
            participant, FullNativeSend(selected, progress), token,
            started.snapshot.require_wire_responses(),
        )

    def tool_owner(self, session, input_id, action) -> CodingToolOwner:
        participant = self.participant
        turn = participant.owner.require_active_turn()
        assignment = self.stage.assignments[0]
        admission = WakeAdmission(
            wire_root_id=participant.root_id,
            source_seq=assignment.wire_seq,
            source_message_id=assignment.message_id,
            wake_assignment_id=assignment.assignment_id,
            wake_revision=assignment.revision,
            recipient_lookup=participant.lookup,
            execution_id=self.stage.execution.require_attempt().execution_id,
            operation_id=action.operation_id(),
            owner_admission_generation=participant.owner.admission_generation,
            turn_id=turn.id,
            participant_generation=participant.identity.generation,
            attempt_ordinal=self.stage.execution.require_attempt().attempt_ordinal,
        )
        return CodingToolOwner(
            participant.comms,
            participant.store,
            admission,
            participant.owner.thread.name,
            session.directory,
            input_id,
        )

    async def prepare(self, session, action, write_authority):
        participant = self.participant
        try:
            action = BatchSelectedAction(tuple(
                (assignment, write_authority.select(
                    assignment,
                    participant.owner.thread,
                    participant.owner.admission_generation,
                    action,
                ))
                for assignment in self.stage.assignments
            ))
            prompt = await SelectedPrompt(participant).full(
                self.stage.assignments, self.obligations, action
            )
            request = SelectedRequest.reserve(
                participant, session, self.stage, self.token, prompt
            )
        except NativePiUnavailable:
            # The attempt exists even if preparation fails before a request can
            # own a reserved ID. Preserve the old dead-attempt UNKNOWN boundary;
            # no previous triage ID is reused for this failed full preparation.
            # A revoked attempt remains with the recovery owner.
            with suppress(StaleFence):
                self.stage.fail_unknown(
                    participant.bus, participant.owner, participant.response_owner
                )
            raise
        return request, action

    async def run(self, execution, session: SelectedSession, action, write_authority):
        participant = self.participant
        request, action = await self.prepare(session, action, write_authority)
        self.stage.progress.input_id = request.admission.input_id
        with request.native_failures():
            tools = self.tool_owner(session, request.admission.input_id, action)
            participant.transition(PromptAcceptancePhase())
            result = await request.admission.execute(
                execution,
                provider=participant.provider,
                model=participant.model,
                observe_event=self.observe_event,
                selected_tool_mode=action.mode(tools),
            )
            result.require_publishable()
            replies = participant.batch.response_messages(result.text, participant.owner.thread.name)
            participant.transition(PublishingPhase())
            action.apply(tools)
            request.admission.commit(participant.store, result.context)
            self.stage.progress.finish()
            participant.owner.require_registry(participant.comms.registry)
            published = await participant.response_owner.publish_responses(
                participant.store.session.path, participant.bus, self.stage.fence, replies
            )
            return await CoordinatedTurn.capture(
                participant, session, request.admission.input_id, CompletedAssignment,
                published.publication_receipts,
            )

    async def observe_event(self, event):
        await self.stage.progress.dispatch(event)
        await self.participant.dispatch(event)


@dataclass(frozen=True)
class SelectedConsideration:
    participant: SelectedParticipant

    async def run(self, execution, session):
        participant = self.participant
        if not participant.batch.requires_triage:
            participant.require_current(participant.store)
            created = participant.store.executions.create(
                participant.batch.execution_id, ExecutionOrigin.WIRE,
                participant.lookup, participant.owner.thread.name, 1,
                sources=participant.batch.sources,
            ).value
            attempt = SelectedAttempt.engage(participant, created)
            return await attempt.run(execution, session,
                                     execution.action(session), execution.write_authority)
        participant.transition(
            PreparingPhase(f"Preparing triage for {len(participant.batch.sources)} messages in {', '.join(participant.batch.targets)}")
        )
        stage = TriageNativeSend(participant.batch.assignments)
        request = SelectedRequest.reserve(
            participant,
            session,
            stage,
            prepare_fence_token(),
            SelectedPrompt(participant).triage(),
        )
        with request.native_failures():
            participant.transition(PromptAcceptancePhase())
            result = await request.admission.execute(
                execution,
                provider=participant.provider,
                model=participant.model,
                observe_event=participant.dispatch,
            )
            participant.transition(PublishingPhase())
            outcome = SelectedTriageOutcome.acquire(result.text)
            settled = outcome.settle(participant, stage, request.admission, result.context)
            continued = session.continued(result.context)
        # The FULL request owns its own failure boundary. Never report its
        # failure through the already-proved triage input's request custody.
        return await outcome.continue_turn(
            participant, continued, request.admission.input_id, execution, settled
        )
