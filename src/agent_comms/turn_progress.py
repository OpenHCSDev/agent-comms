"""Observed turn progress owned by the existing nominal agent-event consumer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

from acp.schema import (
    AgentMessageChunk,
    TextContentBlock,
)

from . import agent_events as events
from .acp_extension import TranscriptChangedUpdate, encode_updates
from .activity import ActivityState
from .channel_targets import is_channel_target
from .comms import Comms
from .diagnostics import record_terminal_failure
from .messages import MessageType
from .mro_dispatch import MroDispatch, handles
from .routing import MessageRoute
from .transcript_updates import AgentTextTranscriptUpdate
from .turn_lease import FinishedTurnFence

if TYPE_CHECKING:
    from .session_lifecycle import SessionLifecycle
    from .turn_effects import TurnEffects
    from .turn_goal_account import TurnGoalAccount


@dataclass(kw_only=True)
class TurnEventPublication(MroDispatch):
    comms: Comms
    sessions: SessionLifecycle
    effects: TurnEffects
    sync_goal_execution: Callable[[str, str], Awaitable[None]]
    session_id: str

    @handles(events.InputStarted, events.Done, events.StreamSettled)
    async def sync_goals(self, event: events.InputStarted) -> None:
        await self.sync_goal_execution(self.session_id, self.sessions.bindings[self.session_id])

    @handles(events.ToolEnd)
    async def tool_result(self, event: events.ToolEnd) -> None:
        await self.sync_goals(event)
        sent = await asyncio.to_thread(
            self.comms.messaging.sent_tool_message,
            event.name,
            event.output,
            bool(event.ok),
        )
        if sent is not None:
            await self.effects._emit_event(
                self.session_id,
                AgentTextTranscriptUpdate(
                    text=sent.body, route=MessageRoute(sent.sender, (sent.target,))
                ),
            )


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
        turn_id,
        turn_lease,
        routing,
        checkpoint,
        task,
        original_keys,
        accepted_input_id,
        initial_display_text,
        finish_event,
        goals: TurnGoalAccount,
        finish_stream,
        sync_goals,
    ):
        self._comms = comms
        self.sessions, self.inputs, self.effects = sessions, inputs, effects
        self.runtime, self.emitted_errors = runtime, emitted_errors
        self.session_id, self.thread = session_id, thread
        self.turn_id, self.turn_lease = turn_id, turn_lease
        self.routing, self.checkpoint, self.task = routing, checkpoint, task
        self.original_keys = original_keys
        self.accepted_input_id, self.initial_display_text = accepted_input_id, initial_display_text
        self.finish_event, self.goals, self.finish_stream = finish_event, goals, finish_stream
        self.reply_parts: list[str] = []
        self.terminal_ok: bool | None = None
        self.terminal_failure: dict[str, Any] = {}
        self.settled = False
        self.terminal_fence: FinishedTurnFence | None = None
        self.cancelled = False
        self.failure_reported = False
        self.compaction_resume_activity: tuple[ActivityState, str] | None = None
        self.publication = TurnEventPublication(
            comms=comms,
            sessions=sessions,
            effects=effects,
            sync_goal_execution=sync_goals,
            session_id=session_id,
        )

    @property
    def origins(self):
        return self.routing.requests

    @property
    def reply_targets(self):
        return self.routing.reply.targets if self.routing.reply is not None else ()

    @property
    def comms(self) -> Comms:
        return self._comms

    @property
    def thread_name(self) -> str:
        return self.sessions.bindings[self.session_id]

    @handles(events.Error)
    async def error_observed(self, event: events.Error) -> None:
        self.failure_reported = True

    async def report_failure(self, error: Exception) -> None:
        """Attempt existing ACP error publication once without hiding the original fault."""
        if self.failure_reported or self.session_id in self.emitted_errors:
            return
        self.failure_reported = True
        try:
            await self.effects._emit_event(
                self.session_id,
                events.Error(str(error)),
                turn_id=self.turn_id,
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

    @handles(events.CompactionStart)
    async def compaction_started(self, event: events.CompactionStart) -> None:
        if self.compaction_resume_activity is None:
            current_activity = self.comms.agents.activity_of(self.thread_name)
            self.compaction_resume_activity = (
                current_activity.state,
                current_activity.detail,
            )
        self.comms.agents.set_activity(
            self.thread_name, ActivityState.WORKING, "Compacting context"
        )

    @handles(events.CompactionEnd)
    async def compaction_ended(self, event: events.CompactionEnd) -> None:
        if self.compaction_resume_activity is not None:
            self.comms.agents.set_activity(self.thread_name, *self.compaction_resume_activity)
            self.compaction_resume_activity = None

    @handles(events.CompactionEvent)
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
        self.terminal_failure = asdict(event)
        unknown_attempts = not self.inputs.dispositions.read().all_started(
            self.inputs.turn_input_keys.get(self.session_id, set())
        )
        if event.ok is True and (
            self.inputs.forwarded_inputs.get(self.session_id) or unknown_attempts
        ):
            # A final assistant stop can prove the original turn,
            # not an ACKed follow-up lacking its own user start.
            event = replace(
                event,
                ok=False,
                text=("An identified follow-up input was not started; inspect local diagnostics."),
            )
        if self.terminal_ok is not None:
            self.terminal_ok = False
        elif event.ok is True:
            self.terminal_ok = True
        else:
            self.terminal_ok = False
        self.goals.done(event)
        if not event.ok and event.text:
            self.failure_reported = True
        return event

    @handles(events.InputStarted)
    async def input_started(self, event: events.InputStarted) -> None:
        await self.inputs.input_started(
            self.session_id,
            event.id if event.id is not None else self.accepted_input_id,
            self.original_keys,
            self.initial_display_text,
        )

    @handles(events.SettingChangeResult)
    async def setting_result(self, event: events.SettingChangeResult) -> None:
        self.sessions.config.setting_requests.resolve(event)

    async def before_agent_info(self, event: events.AgentInfo) -> None:
        session_file = event.session_file
        if (
            session_file
            and self.comms.registry.require(self.thread_name).session_file != session_file
        ):
            self.comms.threads.attach_session(self.thread_name, str(session_file))

    async def after_agent_info(self, event: events.AgentInfo) -> None:
        await self.sessions.config.observe_agent_info(self.session_id, self.thread_name, event)

    @handles(events.ToolEnd)
    async def tool_ended(self, event: events.ToolEnd) -> None:
        await self.sessions.sync_identity(self.session_id)
        self.goals.tool_ended(event)
        self.update_activity(ActivityState.THINKING, self.task[:80])

    @handles(events.StreamSettled)
    async def stream_settled(self, event: events.StreamSettled) -> None:
        self.compaction_resume_activity = None
        self.terminal_fence = self.finish_stream(
            self.session_id, self.thread_name, self.turn_id, self.turn_lease
        )
        self.settled = True
        self.finish_event.set()

    async def consume(self, event: events.AgentEvent) -> None:
        event = await self.dispatch(event)
        await self.effects._emit_event(
            self.session_id, event, turn_id=self.turn_id, route=self.routing.reply
        )
        await self.publication.dispatch(event)

    def update_activity(self, state: ActivityState, detail: str) -> None:
        if self.compaction_resume_activity is not None:
            # Tool notifications may arrive while a summary is in flight.
            # Retain the next activity without hiding active compaction.
            self.compaction_resume_activity = (state, detail)
        else:
            self.comms.agents.set_activity(self.thread_name, state, detail)

    async def publish_result(self):
        self.goals.settle(self.terminal_ok, self.terminal_failure)
        if self.origins and self.settled and self.terminal_ok is True:
            await asyncio.to_thread(
                self.comms.transcripts.record_turn_routing,
                self.thread_name,
                self.checkpoint,
                self.routing,
            )
        if self.terminal_ok is True:
            if self.reply_parts:
                for target in self.reply_targets:
                    self.comms.messaging.send(
                        self.thread_name,
                        target,
                        "".join(self.reply_parts),
                    )
        else:
            # Streamed chunks were provisional. A failed or missing terminal
            # result cannot turn them into a completed wire reply. Keep the
            # failure notice non-waking, including for human reply targets.
            # Backend text may include stderr, secrets, or content from an
            # unrelated session. Persist only structural facts for headless owners.
            diagnostic_path = record_terminal_failure(
                self.comms.root,
                turn_id=self.turn_id,
                thread=self.thread_name,
                event=self.terminal_failure,
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
