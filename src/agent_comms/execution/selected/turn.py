from __future__ import annotations

from contextlib import suppress
from dataclasses import (
    dataclass,
    replace,
)
from functools import partial

from agent_comms.context_segments.selected import (
    SelectedTriageSegment,
    SelectedWorkSegment,
)
from agent_comms.context_segments.wake import SelectedWakeSegment
from agent_comms.coordination.assignment_states import CompletedAssignment
from agent_comms.coordination.attempt_start import AttemptStart
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.durable_turn import DurableTurn
from agent_comms.coordination.errors import (
    CoordinationError,
    IdentityConflict,
    StaleFence,
)
from agent_comms.coordination.optional_awareness_projection import OptionalAwarenessProjection
from agent_comms.coordination.owner_fence import prepare_fence_token
from agent_comms.coordination.snapshot import RecoverySnapshot
from agent_comms.coordination.tables.assignments import WakeAssignment
from agent_comms.coordination.tables.executions import WireOrigin
from agent_comms.coordination.tables.responses import ResponseObligation
from agent_comms.execution.channel_coding_tools import SelectedCodingToolOwner
from agent_comms.execution.coding_child import CodingChild, ToollessMode
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.native_pi import NativeTurnResult
from agent_comms.execution.private_send_stage import (
    FullNativeSend,
    TriageNativeSend,
)
from agent_comms.execution.selected.actions import (
    BatchSelectedAction,
    SelectedAction,
)
from agent_comms.execution.selected.participant import SelectedParticipant
from agent_comms.execution.selected.request import SelectedRequest
from agent_comms.execution.selected.result import CoordinatedTurn
from agent_comms.execution.selected.session import SelectedSession
from agent_comms.execution.selected.triage import (
    RejectedTriageOutcome,
    SelectedTriageOutcome,
)
from agent_comms.execution.turn_context import (
    RecordedContextTurn,
    RenderedInput,
    TurnContext,
)
from agent_comms.execution.turn_goal_account import VerifiedGoalSettlement
from agent_comms.execution.turn_phase import (
    PreparingPhase,
    PromptAcceptancePhase,
    PublishingPhase,
)
from agent_comms.messaging.envelope_claim_transitions import WakeClaim
from agent_comms.waking.wake_candidate_index import WakeCandidateIndex


@dataclass(frozen=True)
class SelectedPrompt:
    participant: SelectedParticipant

    def context(self, segments) -> TurnContext:
        owner = self.participant.owner.thread
        lease = owner.require_turn_lease()
        return TurnContext(
            owner.incarnation,
            RecordedContextTurn(lease.turn_id, lease.identity),
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
            (*frame.input_segments(), SelectedTriageSegment.capture(participant, frame.provenance))
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
                *frame.input_segments(),
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
        snapshot = snapshot.execution.lifecycle.prepare_start(store, snapshot)
        token = prepare_fence_token()
        attempt_ordinal = (snapshot.execution.lifecycle.current_attempt_ordinal or 0) + 1
        started = store.start(
            AttemptStart(
                execution_id=execution_id,
                attempt_ordinal=attempt_ordinal,
                owner_lookup=participant.lookup,
                owner_generation=participant.identity.generation,
                token=token,
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
            thread=participant.owner.thread,
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
            owner = participant.owner
            await Coordination.run_worker(partial(
                VerifiedGoalSettlement.settle_current,
                participant.comms, owner.thread, owner.require_active_turn().id,
            ))
            return await CoordinatedTurn.capture(
                participant, result.context.input_id, CompletedAssignment,
                published.publication_evidence,
            )

    async def observe_event(self, event):
        await self.stage.progress.dispatch(event)
        await self.participant.dispatch(event)
        await self.participant.publication.dispatch(event)


@dataclass(frozen=True)
class TriageAnswer:
    """One triage ask: its decoded outcome, the request that asked, its result and claims."""

    outcome: SelectedTriageOutcome
    request: SelectedRequest
    result: NativeTurnResult
    settled: tuple[WakeAssignment, ...]


@dataclass(frozen=True)
class SelectedConsideration:
    participant: SelectedParticipant

    async def run(self, execution, session):
        participant = self.participant
        if not participant.batch.requires_triage:
            await participant.require_current()
            created = participant.store.create(
                participant.batch.execution_id, WireOrigin,
                participant.lookup,
                execution.max_attempts,
                sources=participant.batch.sources,
            ).value
            return await execution.full(participant, session, created)
        with participant.cursor_delivery.measurements.operation("triage_prompt"):
            await participant.transition(
                PreparingPhase(f"Preparing triage for {len(participant.batch.sources)} messages in {participant.batch.target}")
            )
            stage = TriageNativeSend(participant.batch.assignments)
            prompt = SelectedPrompt(participant).triage()
        request = await SelectedRequest.reserve(
            participant,
            session,
            stage,
            prepare_fence_token(),
            prompt,
        )
        async def asked(request: SelectedRequest) -> tuple[NativeTurnResult, SelectedTriageOutcome]:
            await participant.transition(PromptAcceptancePhase())
            result = await request.claim_scope.execute(
                execution,
                model=participant.model,
                # The ask runs on the child its answer will use, allowing it nothing.
                selected_tool_mode=ToollessMode(
                    CodingChild(comms=participant.comms, thread=participant.owner.thread)),
                observe_event=participant.observe_triage,
            )
            await participant.transition(PublishingPhase())
            return result, SelectedTriageOutcome.acquire(result.text)

        async with request.native_failures():
            result, outcome = await asked(request)
            first = TriageAnswer(outcome, request, result, outcome.close_ask(
                participant, stage, request.claim_scope, result.context))

        async def reask(rejected: RejectedTriageOutcome) -> TriageAnswer:
            # Ask once more with the refusal and the decisions that would have been accepted.
            # The replacement owns its claim revision and its failure boundary: settling it
            # through the first stage or request would fence on the revision the burn and
            # this reservation already moved past.
            refreshed = stage.refreshed(participant.store)
            replacement = await SelectedRequest.reserve(
                participant, session.continued(first.result.context), refreshed,
                prepare_fence_token(),
                replace(request.claim_scope.prompt,
                        text=f"{request.claim_scope.prompt.text}\n\n{rejected.reason()}"),
                attempt=1,
            )
            async with replacement.native_failures():
                result, outcome = await asked(replacement)
                return TriageAnswer(outcome, replacement, result, outcome.settle(
                    participant, refreshed, replacement.claim_scope, result.context))

        answer = await first.outcome.asked_again(first, reask)
        continued = session.continued(answer.result.context)
        # The FULL request owns its own failure boundary. Never report its
        # failure through the already-completed triage request's error handler.
        return await answer.outcome.continue_turn(
            participant, continued, answer.request.claim_scope.input_id, execution, answer.settled
        )
