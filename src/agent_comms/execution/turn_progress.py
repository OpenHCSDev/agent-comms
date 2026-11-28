from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from functools import partial

from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms import agent_events as events
from agent_comms.acp_extension import LiveQueueScope, TranscriptChangedUpdate
from agent_comms.agent_event_updates import RoutedTurnEventConsumer, TurnEventConsumer
from agent_comms.comms import Comms
from agent_comms.coordination.work import CoordinationWork
from agent_comms.execution.acp_failure import FailedNativeTurnOutcome, NativeTurnOutcome
from agent_comms.execution.diagnostics import record_terminal_failure
from agent_comms.execution.native_turn_context import ContextObserved
from agent_comms.execution.publication_measurements import PublicationMeasurements
from agent_comms.execution.request_diagnostics import record_request_progress
from agent_comms.execution.runtime_observations import AgentEventConsumer, NativeMetadataObservation
from agent_comms.execution.selected.triage import SelectedTriage
from agent_comms.execution.turn_failure import ModelRequestFailed, NoTurnFailure, TurnFailure
from agent_comms.execution.turn_lease import TurnState
from agent_comms.execution.turn_phase import NativePhaseChanged, PublishingPhase
from agent_comms.execution.turn_progress_reads import ProgressGoalAccount, ProgressTurn
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.messages import AlertKind, ConversationRole, Message, NoticeRole
from agent_comms.messaging.send_result import SendResult
from agent_comms.nominal.field_codec import AcpSessionId
from agent_comms.registry.thread_identity import ClaimedIdentity
from agent_comms.transcript.transcript_updates import TurnTranscriptUpdate


@dataclass(kw_only=True)
class TurnEventPublication(MroDispatch):
    sync_goal_execution: Callable[[AcpSessionId], Awaitable[None]]
    session_id: AcpSessionId
    @handles(events.InputStarted, events.Done)
    async def sync_goals(self, event: events.InputStarted) -> None:
        await self.sync_goal_execution(self.session_id)




class TurnProgress(AgentEventConsumer[Comms]):
    def __init__(
        self,
        *,
        comms: Comms,
        sessions,
        inputs,
        effects,
        runtime,
        session_id: AcpSessionId,
        turn: ProgressTurn,
        finish_event,
        goals: ProgressGoalAccount,
        sync_goals: Callable[[AcpSessionId], Awaitable[None]],
    ):
        self._comms = comms
        self.sessions, self.inputs, self.effects = sessions, inputs, effects
        self.runtime = runtime
        self.session_id, self.turn = session_id, turn
        self.finish_event, self.goals = finish_event, goals
        self.reply_parts: list[str] = []
        self.delivered_origins: dict[int, Message] = {
            origin.seq: origin for origin in self.origins if origin.seq > 0
        }
        self.terminal_error: events.Error | None = None
        self.terminal_failure: TurnFailure = NoTurnFailure()
        self.publication_measurements = PublicationMeasurements()
        route = self.routing.reply
        self.consumer = (
            RoutedTurnEventConsumer(effects, session_id, runtime, self.turn_lease.turn_id, route)
            if route else TurnEventConsumer(effects, session_id, runtime, self.turn_lease.turn_id)
        )
        self.publication = TurnEventPublication(
            sync_goal_execution=sync_goals,
            session_id=session_id,
        )
        self.transcript_sink = self.thread.backend.turn_transcript_sink(
            self.comms.root,
            self.thread.incarnation,
            self.turn_lease.turn_id,
            tuple(origin.reference for origin in self.origins if origin.seq > 0),
        )

    @property
    def thread(self):
        return self.turn.thread

    @property
    def turn_lease(self):
        return self.turn.turn_lease

    @property
    def routing(self):
        return self.turn.routing

    @property
    def original(self):
        return self.turn.original

    @property
    def checkpoint(self):
        return self.turn.checkpoint

    @property
    def origins(self):
        return self.original.origins

    @property
    def active_reply_targets(self) -> tuple[str, ...]:
        """Initial turn reply targets plus any bus messages steered into this turn."""
        return tuple(dict.fromkeys((
            *self.turn.reply_targets,
            *(
                origin.exact_reply_target
                for origin in self.delivered_origins.values()
                if origin.sender != self.thread_name
            ),
        )))

    @handles(ContextObserved)
    async def observe_context(self, event):
        await CoordinationWork.run_worker(partial(
            self.comms.registry.observe_offered_tools,
            self.thread.incarnation, event.context.offered_tools,
        ))
        manifest = await event.record(self.comms.contexts, self.thread, self.turn_lease)
        self.runtime.agent.annotations.observe(self.session_id, manifest, event.context.values)

    @property
    def thread_name(self) -> str:
        return self.sessions.require(self.session_id)

    async def record_failure(self, failure: TurnFailure) -> events.Done:
        await self.consume(events.Done(text=failure.text, ok=False, failure=failure))
        return self.turn.terminal.require_reported()

    async def report_failure(self, error: Exception, kind: type[TurnFailure]) -> NativeTurnOutcome:
        """Attempt existing ACP error publication once without hiding the original fault."""
        return await self.turn.terminal.report_failure(self, error, kind)


    @handles(events.SteeringInterrupted)
    async def steering_interrupted(self, event: events.SteeringInterrupted) -> None:
        self.reply_parts.clear()

    @handles(events.InputRefused)
    async def input_refused(self, event: events.InputRefused) -> None:
        await event.apply_to(self.inputs, self.session_id)

    @handles(events.ProviderUsage)
    async def provider_usage(self, event: events.ProviderUsage) -> None:
        await CoordinationWork.run_worker(partial(self.goals.provider_usage, event))

    @handles(events.Thinking)
    async def thinking(self, event: events.Thinking) -> None:
        await self.transcript_sink.on_thinking(event.text or "")

    @handles(events.Chunk)
    async def chunk(self, event: events.Chunk) -> None:
        if self.active_reply_targets:
            self.reply_parts.append(event.text)
        await self.transcript_sink.on_chunk(event.text)

    @handles(events.CommittedProgress)
    async def committed_progress(self, event: events.CommittedProgress) -> None:
        await self.report_sends(await CoordinationWork.run_worker(partial(self.publish_progress, event.text)))

    async def report_sends(self, results: Iterable[SendResult]) -> None:
        for result in results:
            for text in result.notices:
                await self.consume(events.Notice(text=text))

    def _publishable_reply_targets(self, text: str) -> tuple[str, ...]:
        targets = self.active_reply_targets
        if SelectedTriage.suppresses_channel_reply(text):
            return tuple(
                kept
                for target in targets
                for kept in CommandTarget.named(target).keep_on_ignore(target)
            )
        return targets

    def publish_progress(self, progress: str) -> tuple[SendResult, ...]:
        """Join the committed notice and consume its streamed text together."""
        results: tuple[SendResult, ...] = ()
        if self.active_reply_targets:
            if progress and "".join(self.reply_parts) == progress:
                # Committed assistant text before tool work or steer handling. Channel
                # targets keep ConversationRole so @mentions wake peers and receipts resolve;
                # DMs keep NoticeRole so intermediate lines never wake a peer mid-turn.
                results = tuple(self.comms.messaging.send(
                    self.thread_name,
                    target,
                    progress,
                    role=CommandTarget.named(target).progress_reply_role(
                        conversation=ConversationRole(), notice=NoticeRole(),
                    ),
                ) for target in self._publishable_reply_targets(progress))
                self.reply_parts.clear()
        if progress:
            self.transcript_sink.on_committed_progress(
                progress, tuple(result.message.reference for result in results)
            )
        return results

    @handles(events.Error)
    async def error(self, event: events.Error) -> object:
        # Raw terminal evidence resolves the later Done; it is not itself a
        # publishable completed outcome. It also names the failure a turn
        # ending without a result keeps, so no caller re-derives it.
        self.terminal_error = event
        self.terminal_failure = ModelRequestFailed.from_diagnostics(
            event.text, event.diagnostics)
        return self.DISCARD

    @handles(events.Done)
    async def done(self, event: events.Done) -> events.Done:
        sent = await CoordinationWork.run_worker(
            partial(self.inputs.dispositions.in_turn, self.turn_lease.turn_id))
        keys = tuple(sent.rows)
        document = await CoordinationWork.run_worker(partial(
            self.inputs.dispositions.read, (*keys, *self.original.keys),
        ))
        unknown_attempts = not document.all_started(keys)
        if event.ok is True and unknown_attempts:
            # A final assistant stop can prove the original turn,
            # not an ACKed follow-up lacking its own user start.
            event = replace(
                event,
                ok=False,
                text=("An identified follow-up input was not started; inspect local diagnostics."),
            )
        if event.ok and not (event.text.strip() or self.goals.has_productive_tool):
            # The declared fact decides, not the output's absence: a turn answering
            # delivered inputs owes the model's reply, so silence is the backend
            # broken. An autonomous scheduled turn (a goal-continue wake) owes no
            # reply — it had no ingress, and its empty completion is the agent
            # declaring nothing was due.
            if self.original.origins:
                event = replace(
                    event,
                    ok=False,
                    text="Backend reported success without assistant output or tool activity.",
                )
        event = self.turn.terminal.reported(event)
        event = event.resolve(self.terminal_error, partial(
            FailedNativeTurnOutcome.resolve, document=document, original=self.original,
        ))
        # Retain the resolved outcome before transport publication. If delivery
        # throws, failure handling must not classify or resolve it a second time.
        self.turn.terminal = event
        return event

    @handles(events.InputStarted)
    async def input_started(self, event: events.InputStarted) -> None:
        captured = event.captured(self.inputs, self.session_id)
        await captured.started(
            self.inputs, self.session_id,
            LiveQueueScope(session_id=self.session_id, claimed_identity=ClaimedIdentity(
                self.turn_lease.identity.incarnation,
                self.turn_lease.claimed_generation,
            ), owner=self.thread.require_process()),
        )
        for origin in captured.origins:
            if origin.seq > 0:
                self.delivered_origins[origin.seq] = origin
        await CoordinationWork.run_worker(partial(
            self.thread.backend.acknowledge_turn_inputs, self.comms, self.thread, captured.origins,
        ))

    async def before_native_metadata(self, event: NativeMetadataObservation) -> None:
        await event.session_file.attach(self.turn.attach_native_session)

    async def after_native_metadata(self, event: NativeMetadataObservation) -> None:
        await self.sessions.observe_native_configuration(self.session_id, self.thread_name, event)

    @handles(events.ToolStart)
    async def tool_started(self, event: events.ToolStart) -> None:
        await self.transcript_sink.on_tool_start(event)

    @handles(events.ToolEnd)
    async def tool_ended(self, event: events.ToolEnd) -> None:
        await self.transcript_sink.on_tool_end(event)
        await self.sessions.sync_identity(self.session_id)
        await CoordinationWork.run_worker(partial(self.goals.tool_ended, event))

    async def transition(self, phase) -> None:
        states = await CoordinationWork.run_worker(partial(
            self.comms.agents.transition_turn, self.turn_lease, phase
        ))
        await self.emit_turn_effects(states)

    async def emit_turn_effects(self, states: Iterable[TurnState]) -> None:
        for state in states:
            with self.publication_measurements.measuring():
                await self.consumer.dispatch(TurnTranscriptUpdate(state=state))

    @handles(NativePhaseChanged)
    async def native_phase(self, event: NativePhaseChanged) -> None:
        states = await CoordinationWork.run_worker(partial(
            self.comms.agents.observe_native_phase, self.turn_lease, event.phase
        ))
        await self.emit_turn_effects(states)

    def record_request_progress(self, progress, native_process) -> None:
        record_request_progress(self.comms.root, self.turn_lease, progress,
                                native_process=native_process,
                                publication=self.publication_measurements)

    @handles(events.StreamSettled)
    async def stream_settled(self, event: events.StreamSettled) -> None:
        await self.transition(PublishingPhase())
        self.finish_event.set()

    async def consume(self, event: events.AgentEvent) -> None:
        publication = await self.dispatch(event)
        if publication is None:
            return
        with self.publication_measurements.measuring():
            published = await self.consumer.dispatch(publication)
        # ACP publication may replace Done with its notification acknowledgement;
        # terminal consumers retain that exact completed outcome.
        published.retain_terminal(self.turn)
        await self.publication.dispatch(published)

    def publish_success(self) -> tuple[SendResult, ...]:
        """Join each acquired original send before releasing the turn lease."""
        published: list[SendResult] = []

        def record_publication() -> None:
            published_refs = tuple(result.message.reference for result in published)
            self.transcript_sink.on_finish(published_refs)
            self.comms.transcripts.record_turn_publication(
                lease=self.turn_lease,
                checkpoint=self.checkpoint,
                routing=self.routing,
                published=published_refs,
            )

        with ExitStack() as publications:
            publications.callback(record_publication)
            if self.reply_parts:
                reply = "".join(self.reply_parts)
                for target in self._publishable_reply_targets(reply):
                    published.append(self.comms.messaging.send(
                        self.thread_name, target, reply
                    ))
        self._settle_unanswered_channel_origins()
        self.comms.agents.recover_completed_turn(self.turn_lease, self.thread.backend)
        return tuple(published)

    def _settle_unanswered_channel_origins(self) -> None:
        """Settle channel messages delivered to this turn that received no reply as Not answering."""
        channel_origins = tuple(
            settled
            for origin in self.delivered_origins.values()
            if origin.sender != self.thread_name
            for settled in CommandTarget.named(origin.target).settle_unanswered(origin)
        )
        if not channel_origins:
            return
        lookup = self.thread.incarnation.require_recorded().lookup
        answered_seqs = frozenset(
            reply.original
            for reply in self.comms.bus.bus.recipient_replies(
                tuple((lookup, origin.reference) for origin in channel_origins)
            )
        )
        unanswered = tuple(
            origin for origin in channel_origins if origin.seq not in answered_seqs
        )
        if unanswered:
            self.thread.backend.ignore_turn_inputs(self.comms, self.thread, unanswered)

    def publish_checkpoint(self, terminal: events.Done) -> tuple[SendResult, ...]:
        """Close terminal publication and its original read before ACP delivery."""
        if terminal.ok:
            return self.publish_success()
        else:
            self.transcript_sink.on_abort()
            # Streamed chunks were provisional. A failed or missing terminal
            # result cannot turn them into a completed wire reply. Keep the
            # failure notice non-waking, including for human reply targets.
            notice_targets = tuple(
                dict.fromkeys(
                    (*self.active_reply_targets,)
                    + tuple(
                        origin.exact_reply_target
                        for origin in self.origins
                        if origin.sender != self.thread_name
                    )
                )
            )
            diagnostic = record_terminal_failure(
                self.comms.root,
                subject=self.turn_lease.turn_id,
                thread=self.thread_name,
                failure=terminal.turn_failure,
                measurements=terminal.diagnostic,
                sequences=(),
            )
            results = []
            for target in notice_targets:
                prefix = (
                    "Request failed" if target in self.turn.reply_targets else "Delivery failed"
                )
                results.append(self.comms.messaging.send(
                    self.thread_name,
                    target,
                    f"{prefix}: {terminal.text} "
                    f"No automatic retry. [Open diagnostic]({diagnostic.as_uri()})",
                    AlertKind,
                    role=NoticeRole(),
                ))
            return tuple(results)

    async def publish_result(self):
        terminal = await self.turn.terminal.prepare_publication(self)
        # Cancellation must join original sends, their annotation and the
        # checkpoint before OwnedTurn can settle or release its lease.
        await self.report_sends(await CoordinationWork.run_worker(
            partial(self.publish_checkpoint, terminal)
        ))
        checkpoint = await CoordinationWork.run_worker(partial(
            self.comms.transcripts.transcript_checkpoint, self.thread_name))
        await self.runtime.publish_facts(
            TranscriptChangedUpdate(checkpoint), session_id=self.session_id,
        )
        terminal.outcome.raise_if_failed()
