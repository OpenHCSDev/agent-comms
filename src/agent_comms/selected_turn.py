"""Selected triage and full attempts consume actual reservation and lifecycle owners."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial

from .attempt_start import AttemptStart
from .assignment_states import CompletedAssignment
from .channel_coding_tools import SelectedCodingToolOwner
from .coordination_errors import CoordinationError, IdentityConflict, StaleFence
from .coordinator import Coordination
from .coordination_tables.executions import ExecutionOrigin
from .coordination_snapshot import RecoverySnapshot
from .coordination_tables.responses import ResponseObligation
from .durable_turn import DurableTurn
from .envelope_claim_transitions import WakeClaim
from .optional_awareness_projection import OptionalAwarenessProjection
from .native_failures import NativePiUnavailable
from .owner_fence import prepare_fence_token
from .private_send_stage import FullNativeSend, TriageNativeSend
from .selected_actions import BatchSelectedAction, NoSelectedTools, SelectedAction
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

    def sent_outcomes(self):
        return self.participant.sent_outcomes[:SelectedWakeSegment.sent_limit]

    def triage(self) -> RenderedInput:
        participant = self.participant
        frame = SelectedWakeSegment.capture(
            tuple((source.delivery, source.assignment) for source in participant.batch.sources),
            participant.owner.thread,
            participant.comms.goals,
            sent_outcomes=self.sent_outcomes(),
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
            participant.comms.goals,
            obligations=obligations,
            sent_outcomes=self.sent_outcomes(),
        )
        projection = OptionalAwarenessProjection.for_selected(
            WakeCandidateIndex(participant.bus.bus),
            registry=participant.bus._registry,
            through_seq=participant.batch.sources[-1].delivery.message.seq,
            generation=participant.identity.generation,
            claimed_generation=participant.owner.claimed_generation,
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
                participant.batch.response_segment(),
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
    async def engage(cls, participant: SelectedParticipant,
               snapshot: RecoverySnapshot) -> SelectedAttempt:
        await participant.prepare_attempt(snapshot)
        store = participant.store
        assignment_ids = participant.batch.assignment_ids
        execution_id = participant.batch.execution_id
        if snapshot.execution.execution_id != execution_id:
            raise IdentityConflict("selected attempt differs from its created batch execution")
        if snapshot.execution.lifecycle.queued:
            snapshot = store.mark_pending(
                execution_id,
                expected_revision=snapshot.execution.revision,
            ).value
        token = prepare_fence_token()
        attempt_ordinal = (snapshot.execution.lifecycle.current_attempt_ordinal or 0) + 1
        started = store.start(
            AttemptStart(
                execution_id,
                attempt_ordinal,
                participant.owner.thread.name,
                participant.identity.generation,
                token,
                expected_execution_revision=snapshot.execution.revision,
                expected_pointer_revision=snapshot.pointer_revision,
            )
        ).value
        progress = DurableTurn(store, started.fence, started.snapshot.pointer_revision, "")
        selected = started.snapshot.assignments
        if tuple(row.assignment_id for row in selected) != assignment_ids:
            raise IdentityConflict("full wake lost an original batch assignment")
        await participant.transition(
            PreparingPhase(f"Preparing response to {len(selected)} messages in {participant.batch.target}")
        )
        return cls(
            participant, FullNativeSend(selected, progress), token,
            started.snapshot.require_wire_responses(),
        )

    def tool_owner(self, session, input_id, action) -> SelectedCodingToolOwner:
        participant = self.participant
        turn = participant.owner.require_active_turn()
        assignment = self.stage.assignments[0]
        claim_scope = WakeClaim(
            wire_root_id=participant.root_id,
            source_seq=assignment.wire_seq,
            source_message_id=assignment.message_id,
            wake_assignment_id=assignment.assignment_id,
            wake_revision=assignment.revision,
            recipient_lookup=participant.lookup,
            execution_id=self.stage.execution.require_attempt().execution_id,
            operation_id=action.operation_id(),
            owner_claimed_generation=participant.owner.claimed_generation,
            turn_id=turn.id,
            participant_generation=participant.identity.generation,
            attempt_ordinal=self.stage.execution.require_attempt().attempt_ordinal,
        )
        return SelectedCodingToolOwner(
            comms=participant.comms,
            store=participant.store,
            resource_scope=claim_scope,
            owner_name=participant.owner.thread.name,
            session_dir=session.directory,
            input_id=input_id,
            goal_permission=participant.goal_permission,
        )

    async def prepare(self, session, action, write_authority):
        participant = self.participant
        try:
            action = BatchSelectedAction(tuple(
                (
                    assignment,
                    write_authority.select(
                        assignment,
                        participant.owner.thread,
                        participant.owner.claimed_generation,
                        action,
                    ).prepare(participant.owner.thread),
                )
                for assignment in self.stage.assignments
            ))
            prompt = await SelectedPrompt(participant).full(
                self.stage.assignments, self.obligations, action
            )
            request = await SelectedRequest.reserve(
                participant, session, self.stage, self.token, prompt
            )
        except (NativePiUnavailable, CoordinationError, OSError, ValueError):
            # The attempt exists even if preparation fails before a request can
            # own a reserved ID (native unavailability, or a busy journal or
            # coordinator store). Preserve the old dead-attempt UNKNOWN boundary
            # so the owner's pointer does not stay on a wedged attempt; no
            # previous triage ID is reused for this failed full preparation.
            # A revoked attempt remains with the recovery owner.
            with suppress(StaleFence):
                await self.stage.fail_unknown(
                    participant.bus, participant.owner, participant.response_owner
                )
            raise
        return request, action

    async def run(self, execution, session: SelectedSession, action, write_authority):
        participant = self.participant
        request, action = await self.prepare(session, action, write_authority)
        self.stage.progress.input_id = request.claim_scope.input_id
        async with request.native_failures():
            tools = self.tool_owner(session, request.claim_scope.input_id, action)
            await participant.transition(PromptAcceptancePhase())
            result = await request.claim_scope.execute(
                execution,
                model=participant.model,
                observe_event=self.observe_event,
                selected_tool_mode=action.mode(tools),
                # Only the full answer takes later inputs; a triage call answers one question.
                following=execution.following,
            )
            result.require_publishable()
            replies = (participant.batch.response_message(result.text, participant.owner.thread.name),)
            await participant.transition(PublishingPhase())
            await Coordination.run_async(
                participant.store.session.path,
                lambda store: action.apply(replace(tools, store=store)),
            )
            request.claim_scope.commit(participant.store, result.context)
            self.stage.progress.finish()
            await Coordination.run_worker(partial(
                participant.owner.require_running_turn, participant.comms.registry
            ))
            published = await participant.response_owner.publish_responses(
                participant.store.session.path, participant.bus, self.stage.fence, replies
            )
            return await CoordinatedTurn.capture(
                participant, result.context.input_id, CompletedAssignment,
                published.publication_evidence,
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
            await participant.require_current()
            created = participant.store.create(
                participant.batch.execution_id, ExecutionOrigin.WIRE,
                participant.lookup, participant.owner.thread.name,
                execution.max_attempts,
                sources=participant.batch.sources,
            ).value
            return await execution.full(participant, session, created)
        await participant.transition(
            PreparingPhase(f"Preparing triage for {len(participant.batch.sources)} messages in {participant.batch.target}")
        )
        stage = TriageNativeSend(participant.batch.assignments)
        request = await SelectedRequest.reserve(
            participant,
            session,
            stage,
            prepare_fence_token(),
            SelectedPrompt(participant).triage(),
        )
        async with request.native_failures():
            await participant.transition(PromptAcceptancePhase())
            result = await request.claim_scope.execute(
                execution,
                model=participant.model,
                selected_tool_mode=NoSelectedTools(),
                observe_event=participant.dispatch,
            )
            await participant.transition(PublishingPhase())
            outcome = SelectedTriageOutcome.acquire(result.text)
            settled = outcome.settle(participant, stage, request.claim_scope, result.context)
            continued = session.continued(result.context)
        # The FULL request owns its own failure boundary. Never report its
        # failure through the already-completed triage request's error handler.
        return await outcome.continue_turn(
            participant, continued, request.claim_scope.input_id, execution, settled
        )
