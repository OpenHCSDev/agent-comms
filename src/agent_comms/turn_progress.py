"""Observed turn progress owned by the existing nominal agent-event consumer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from acp.schema import (
    AgentMessageChunk,
    TextContentBlock,
)

from . import agent_events as events
from .acp_extension import QueueScope, TranscriptChangedUpdate, encode_updates
from .thread_identity import AdmissionIdentity
from .channel_targets import is_channel_target
from .comms import Comms
from .diagnostics import PublicationMeasurements, record_terminal_failure, record_request_progress
from .messages import MessageType
from .message_reference import MessageReference
from .mro_dispatch import MroDispatch, handles
from .native_input_owner import RegistryOwner
from .turn_phase import PublishingPhase
from .transcript_updates import TurnTranscriptUpdate

if TYPE_CHECKING:
    from .session_lifecycle import SessionLifecycle
    from .turn_effects import TurnEffects
    from .turn_goal_account import TurnGoalAccount
    from .turn_input_source import OriginalTurnInput


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
        emitted_errors,
        session_id,
        thread,
        turn_lease,
        routing,
        original: OriginalTurnInput,
        checkpoint,
        finish_event,
        goals: TurnGoalAccount,
        sync_goals,
    ):
        self._comms = comms
        self.sessions, self.inputs, self.effects = sessions, inputs, effects
        self.runtime, self.emitted_errors = runtime, emitted_errors
        self.session_id, self.thread = session_id, thread
        self.turn_lease = turn_lease
        self.routing, self.checkpoint = routing, checkpoint
        self.original = original
        self.finish_event, self.goals = finish_event, goals
        self.reply_parts: list[str] = []
        self.result: events.Done | None = None
        self.publication_measurements = PublicationMeasurements()
        self.publication = TurnEventPublication(
            comms=comms,
            sessions=sessions,
            effects=effects,
            sync_goal_execution=sync_goals,
            session_id=session_id,
        )

    @property
    def origins(self):
        return self.original.origins

    @handles(events.ContextObserved)
    async def observe_context(self, event):
        await event.context.record(self.comms.bus.log, self.thread, self.turn_lease)

    @property
    def reply_targets(self):
        return self.routing.reply.targets if self.routing.reply is not None else ()

    @property
    def comms(self) -> Comms:
        return self._comms

    @property
    def thread_name(self) -> str:
        return self.sessions.bindings[self.session_id]

    async def report_failure(self, error: Exception) -> None:
        """Attempt existing ACP error publication once without hiding the original fault."""
        prior = self.emitted_errors.get(self.session_id)
        diagnostic = record_terminal_failure(
            self.comms.root, turn_id=self.turn_lease.turn_id, thread=self.thread_name,
            event=asdict(self.result) if self.result is not None else {},
            sequences=tuple(origin.seq for origin in self.routing.requests),
            source_error=error,
        )
        detail = prior.detail if prior is not None else (
            "Native turn failed; original input was not retried. "
            f"[Open diagnostic]({diagnostic.as_uri()})"
        )
        try:
            await self.effects._emit_event(
                self.session_id,
                events.Error(detail),
                turn_id=self.turn_lease.turn_id,
                route=self.routing.reply,
            )
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
        self.goals.provider_usage(event)

    @handles(events.CompactionStart, events.CompactionEnd)
    async def invalidate_context(self, event: events.CompactionEvent) -> None:
        info = self.comms.agents.agent_info_of(self.thread_name)
        self.comms.agents.set_agent_info(
            self.thread_name,
            model=info.model if info else None,
            session_name=info.session_name if info else None,
            context_used=None,
            context_size=info.context_size if info else None,
        )

    @handles(events.Chunk)
    async def chunk(self, event: events.Chunk) -> None:
        if self.reply_targets:
            self.reply_parts.append(event.text)

    @handles(events.CommittedProgress)
    async def committed_progress(self, event: events.CommittedProgress) -> None:
        if self.reply_targets:
            progress = event.text
            if progress and "".join(self.reply_parts) == progress:
                # Pi committed this assistant message before tool work.
                # Publish it once as visible, non-waking progress; the
                # final reply contains only subsequent assistant text.
                for target in self.reply_targets:
                    self.comms.messaging.send(self.thread_name, target, progress, notice=True)
                self.reply_parts.clear()

    @handles(events.Done)
    async def done(self, event: events.Done) -> events.Done:
        unknown_attempts = not self.inputs.dispositions.read().all_started(
            self.inputs.turn_input_keys.get(self.session_id, set())
        )
        if event.ok is True and (
            self.inputs.pending_followups(self.session_id) or unknown_attempts
        ):
            # A final assistant stop can prove the original turn,
            # not an ACKed follow-up lacking its own user start.
            event = replace(
                event,
                ok=False,
                text=("An identified follow-up input was not started; inspect local diagnostics."),
            )
        if self.result is not None:
            event = replace(event, ok=False, text="Duplicate native terminal result")
        self.result = event
        self.goals.done(event)
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

    @handles(events.SettingChangeResult)
    async def setting_result(self, event: events.SettingChangeResult) -> None:
        self.sessions.config.setting_requests.resolve(event)

    async def before_agent_info(self, event: events.AgentInfo) -> None:
        session_file = event.session_file
        if session_file and self.thread.session_file != session_file:
            self.thread = self.comms.registry.attach_native_session(
                RegistryOwner(thread=self.thread,
                              admission_generation=self.turn_lease.admission_generation),
                str(session_file),
            ).thread

    async def after_agent_info(self, event: events.AgentInfo) -> None:
        await self.sessions.observe_native_configuration(self.session_id, self.thread_name, event)

    @handles(events.ToolEnd)
    async def tool_ended(self, event: events.ToolEnd) -> None:
        await self.sessions.sync_identity(self.session_id)
        self.goals.tool_ended(event)

    @property
    def phase(self):
        return self.comms.registry.require(self.thread_name).turn_state.phase

    async def transition(self, phase) -> None:
        if self.phase == phase:
            return
        if self.comms.agents.transition_turn(self.turn_lease, phase):
            state = self.comms.registry.require(self.thread_name).turn_state
            with self.publication_measurements.measuring():
                await self.effects._emit_event(self.session_id, TurnTranscriptUpdate(state=state))

    @handles(events.NativePhaseChanged)
    async def native_phase(self, event: events.NativePhaseChanged) -> None:
        await self.transition(self.phase.observed(event.phase))

    def record_request_progress(self, progress, native_process) -> None:
        record_request_progress(self.comms.root, self.turn_lease, progress,
                                native_process=native_process,
                                publication=self.publication_measurements)

    @handles(events.StreamSettled)
    async def stream_settled(self, event: events.StreamSettled) -> None:
        await self.transition(PublishingPhase())
        self.finish_event.set()

    async def consume(self, event: events.AgentEvent) -> None:
        event = await self.dispatch(event)
        with self.publication_measurements.measuring():
            await self.effects._emit_event(
                self.session_id, event, turn_id=self.turn_lease.turn_id, route=self.routing.reply
            )
        await self.publication.dispatch(event)

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

    async def publish_result(self):
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
                    MessageType.ALERT,
                    notice=True,
                )
        await self.runtime.session_update(
            session_id=self.session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=encode_updates(
                    TranscriptChangedUpdate(
                        self.comms.transcripts.transcript_checkpoint(self.thread_name)
                    )
                ),
            ),
        )
