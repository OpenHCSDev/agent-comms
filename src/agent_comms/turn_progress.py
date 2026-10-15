"""Observed turn progress owned by the existing nominal agent-event consumer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from acp.schema import (
    AgentMessageChunk,
    TextContentBlock,
)

from . import agent_events as events
from .acp_extension import QueueScope, TranscriptChangedUpdate, encode_updates
from .acp_failure import RequestFailure
from .thread_identity import AdmissionIdentity
from .channel_targets import is_channel_target
from .comms import Comms
from .coordinator import Coordination
from .diagnostics import PublicationMeasurements, record_terminal_failure, record_request_progress
from .messages import AlertKind
from .message_reference import MessageReference
from .mro_dispatch import MroDispatch, handles
from .turn_phase import PublishingPhase
from .transcript_updates import TurnTranscriptUpdate
from .turn_lease import TurnState

if TYPE_CHECKING:
    from .session_lifecycle import SessionLifecycle
    from .turn_effects import TurnEffects
    from .turn_goal_account import TurnGoalAccount
    from .owned_turn import OwnedTurn


@dataclass(kw_only=True)
class TurnEventPublication(MroDispatch):
    comms: Comms
    sessions: SessionLifecycle
    effects: TurnEffects
    sync_goal_execution: Callable[[str, str], Awaitable[None]]
    session_id: str
    @handles(events.InputStarted, events.Done, events.StreamSettled, events.ToolEnd)
    async def sync_goals(self, event: events.InputStarted) -> None:
        await self.sync_goal_execution(self.session_id, self.sessions.bindings[self.session_id])




class TurnProgress(events.AgentEventConsumer):
    def __init__(
        self,
        *,
        comms,
        sessions,
        inputs,
        effects,
        runtime,
        session_id,
        turn: OwnedTurn,
        finish_event,
        goals: TurnGoalAccount,
        sync_goals,
    ):
        self._comms = comms
        self.sessions, self.inputs, self.effects = sessions, inputs, effects
        self.runtime = runtime
        self.session_id, self.turn = session_id, turn
        self.finish_event, self.goals = finish_event, goals
        self.reply_parts: list[str] = []
        self.result: events.Done | None = None
        self.terminal_error: events.Error | None = None
        self.publication_measurements = PublicationMeasurements()
        self.publication = TurnEventPublication(
            comms=comms,
            sessions=sessions,
            effects=effects,
            sync_goal_execution=sync_goals,
            session_id=session_id,
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

    @handles(events.ContextObserved)
    async def observe_context(self, event):
        manifest = await event.context.record(self.comms.bus.log, self.thread, self.turn_lease)
        self.runtime.agent.annotations.observe(self.session_id, manifest, event.context.values)

    @property
    def reply_targets(self):
        return self.routing.reply.targets if self.routing.reply is not None else ()

    @property
    def comms(self) -> Comms:
        return self._comms

    @property
    def thread_name(self) -> str:
        return self.sessions.bindings[self.session_id]

    async def report_failure(self, error: Exception) -> RequestFailure | None:
        """Attempt existing ACP error publication once without hiding the original fault."""
        diagnostic = record_terminal_failure(
            self.comms.root, turn_id=self.turn_lease.turn_id, thread=self.thread_name,
            event=asdict(self.result) if self.result is not None else {},
            sequences=tuple(origin.seq for origin in self.routing.requests),
            source_error=error,
        )
        detail = (
            "Native turn failed. "
            f"[Open diagnostic]({diagnostic.as_uri()})"
        )
        if self.result is not None:
            # A transport/cleanup fault cannot reconstruct an already resolved
            # native failure. Keep the reported failure and separate diagnostic.
            return self.result.outcome.request_failure
        try:
            await self.consume(events.Done(text=detail, ok=False))
            return self.result.outcome.request_failure
        except Exception as delivery_error:
            self.effects._debug_log(
                f"turn:error-publication failed: {delivery_error!r}; original: {error!r}"
            )


    @handles(events.SteeringInterrupted)
    async def steering_interrupted(self, event: events.SteeringInterrupted) -> None:
        self.reply_parts.clear()

    @handles(events.InputRefused)
    async def input_refused(self, event: events.InputRefused) -> None:
        await self.inputs.input_refused(self.session_id, event.id)

    @handles(events.ProviderUsage)
    async def provider_usage(self, event: events.ProviderUsage) -> None:
        await Coordination.run_worker(partial(self.goals.provider_usage, event))

    @handles(events.Chunk)
    async def chunk(self, event: events.Chunk) -> None:
        if self.reply_targets:
            self.reply_parts.append(event.text)

    @handles(events.CommittedProgress)
    async def committed_progress(self, event: events.CommittedProgress) -> None:
        await Coordination.run_worker(partial(self.publish_progress, event.text))

    def publish_progress(self, progress: str) -> None:
        """Join the committed notice and consume its streamed text together."""
        if self.reply_targets:
            if progress and "".join(self.reply_parts) == progress:
                # Pi committed this assistant message before tool work.
                # Publish it once as visible, non-waking progress; the
                # final reply contains only subsequent assistant text.
                for target in self.reply_targets:
                    self.comms.messaging.send(self.thread_name, target, progress, notice=True)
                self.reply_parts.clear()

    @handles(events.Error)
    async def error(self, event: events.Error) -> object:
        # Raw terminal evidence resolves the later Done; it is not itself a
        # publishable completed outcome.
        self.terminal_error = event
        return self.DISCARD

    @handles(events.Done)
    async def done(self, event: events.Done) -> events.Done:
        keys = self.inputs.input_keys(self.session_id)
        document = await Coordination.run_worker(
            partial(self.inputs.dispositions.read, (*keys, *self.original.notice_keys)))
        unknown_attempts = not document.all_started(keys)
        if event.ok is True and unknown_attempts:
            # A final assistant stop can prove the original turn,
            # not an ACKed follow-up lacking its own user start.
            event = replace(
                event,
                ok=False,
                text=("An identified follow-up input was not started; inspect local diagnostics."),
            )
        if self.result is not None:
            event = replace(event, ok=False, text="Duplicate native terminal result")
        event = event.resolve(self.terminal_error, document, self.original)
        # Retain the resolved outcome before transport publication. If delivery
        # throws, failure handling must not classify or resolve it a second time.
        self.result = event
        await Coordination.run_worker(partial(self.goals.done, event))
        return event

    @handles(events.InputStarted)
    async def input_started(self, event: events.InputStarted) -> None:
        await self.inputs.input_started(
            self.session_id,
            event.id,
            QueueScope(self.session_id, AdmissionIdentity(
                self.turn_lease.identity.incarnation,
                self.turn_lease.admission_generation,
            ), self.thread.pid),
        )

    async def before_native_metadata(self, event: events.NativeMetadataObservation) -> None:
        session_file = event.session_file
        if session_file:
            await self.turn.attach_native_session(session_file)

    async def after_native_metadata(self, event: events.NativeMetadataObservation) -> None:
        await self.sessions.observe_native_configuration(self.session_id, self.thread_name, event)

    @handles(events.ToolEnd)
    async def tool_ended(self, event: events.ToolEnd) -> None:
        await self.sessions.sync_identity(self.session_id)
        await Coordination.run_worker(partial(self.goals.tool_ended, event))

    async def transition(self, phase) -> None:
        states = await Coordination.run_worker(partial(
            self.comms.agents.transition_turn, self.turn_lease, phase
        ))
        await self.emit_turn_effects(states)

    async def emit_turn_effects(self, states: Iterable[TurnState]) -> None:
        for state in states:
            with self.publication_measurements.measuring():
                await self.effects._emit_event(self.session_id, TurnTranscriptUpdate(state=state))

    @handles(events.NativePhaseChanged)
    async def native_phase(self, event: events.NativePhaseChanged) -> None:
        states = await Coordination.run_worker(partial(
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
            published = await self.effects._emit_event(
                self.session_id, publication,
                turn_id=self.turn_lease.turn_id, route=self.routing.reply,
            )
        if isinstance(published, events.Done):
            # ACP publication may replace Done with its notification receipt;
            # terminal consumers retain that exact completed outcome.
            self.result = published
        await self.publication.dispatch(published)

    def publish_success(self) -> None:
        """Join each acquired original send before releasing the turn lease."""
        published: list[MessageReference] = []

        def record_publication() -> None:
            self.comms.transcripts.record_turn_publication(
                lease=self.turn_lease,
                checkpoint=self.checkpoint,
                routing=self.routing,
                published=tuple(published),
            )

        with ExitStack() as publications:
            publications.callback(record_publication)
            if self.reply_parts:
                for target in self.reply_targets:
                    message = self.comms.messaging.send_message(
                        self.thread_name, target, "".join(self.reply_parts)
                    )
                    published.append(message.reference)

    def publish_checkpoint(self):
        """Close terminal publication and its original read before ACP delivery."""
        if self.result is not None and self.result.ok:
            self.publish_success()
        else:
            # Streamed chunks were provisional. A failed or missing terminal
            # result cannot turn them into a completed wire reply. Keep the
            # failure notice non-waking, including for human reply targets.
            # Backend text may include stderr, secrets, or content from an
            # unrelated session. Persist only structural facts for headless owners.
            diagnostic_path = record_terminal_failure(
                self.comms.root,
                turn_id=self.turn_lease.turn_id,
                thread=self.thread_name,
                event=asdict(self.result) if self.result is not None else {},
                sequences=tuple(origin.seq for origin in self.origins),
            )
            notice_targets = tuple(
                dict.fromkeys(
                    (*self.reply_targets,)
                    + tuple(
                        origin.target if is_channel_target(origin.target) else origin.sender
                        for origin in self.origins
                        if origin.sender != self.thread_name
                    )
                )
            )
            for target in notice_targets:
                prefix = "Request failed" if target in self.reply_targets else "Delivery failed"
                self.comms.messaging.send(
                    self.thread_name,
                    target,
                    f"{prefix}: backend turn did not complete. "
                    f"[Open diagnostic]({diagnostic_path.as_uri()})",
                    AlertKind,
                    notice=True,
                )
        return self.comms.transcripts.transcript_checkpoint(self.thread_name)

    async def publish_result(self):
        if self.result is None:
            await self.consume(events.Done(text="Native turn ended without a result", ok=False))
        # Cancellation must join original sends, their annotation and the
        # checkpoint before OwnedTurn can settle or release its lease.
        checkpoint = await Coordination.run_worker(self.publish_checkpoint)
        await self.runtime.session_update(
            session_id=self.session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(
                    TranscriptChangedUpdate(checkpoint)
                ),
            ),
        )
        self.result.outcome.raise_if_failed()
