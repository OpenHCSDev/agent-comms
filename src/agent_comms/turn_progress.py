"""Observed turn progress owned by the existing nominal agent-event consumer."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from acp.schema import (
    AgentMessageChunk,
    TextContentBlock,
)

from . import agent_events as events
from .activity import ActivityState
from .channel_targets import is_channel_target
from .comms import Comms
from .diagnostics import record_terminal_failure, terminal_failure_reason
from .goal_actions import BlockedGoalAction, GoalPrecondition
from .goal_attempts import LaunchPermit, StaleAttemptError
from .goal_failure_observation import FailedTurnObservation
from .goal_states import ActiveGoal, CompletedGoal, PausedGoal
from .messages import MessageType
from .mro_dispatch import MroDispatch, handles
from .pi_payloads import PiUsage
from .routing import MessageRoute
from .transcript_updates import AgentTextTranscriptUpdate
from .turn_lease import FinishedTurnFence

if TYPE_CHECKING:
    from .owned_turn import OwnedTurn


class TurnEventPublication(MroDispatch):
    def __init__(self, execution: OwnedTurn):
        self.execution = execution

    @handles(events.InputStarted, events.Done, events.StreamSettled)
    async def sync_goals(self, event: events.InputStarted) -> None:
        execution = self.execution
        await execution.runner.sync_goal_execution(execution.session_id, execution.thread_name)

    @handles(events.ToolEnd)
    async def tool_result(self, event: events.ToolEnd) -> None:
        execution = self.execution
        await execution.runner.sync_goal_execution(execution.session_id, execution.thread_name)
        sent = await asyncio.to_thread(
            execution.runner.comms.messaging.sent_tool_message,
            event.name,
            event.output,
            bool(event.ok),
        )
        if sent is not None:
            await execution.runner.effects._emit_event(
                execution.session_id,
                AgentTextTranscriptUpdate(
                    text=sent.body, route=MessageRoute(sent.sender, (sent.target,))
                ),
            )


class TurnProgress(events.AgentEventConsumer):
    def __init__(self, execution: OwnedTurn):
        self.execution = execution
        self.reply_parts: list[str] = []
        self.terminal_ok: bool | None = None
        self.terminal_failure: dict[str, Any] = {}
        self.successful_tool_observed = False
        self.goal_attempt_resolved = False
        self.originated_goal_ids: set[str] = set()
        self.originated_attempts: dict[str, LaunchPermit] = {}
        self.unattributed_usage: list[tuple[str, PiUsage]] = []
        self.settled = False
        self.terminal_fence: FinishedTurnFence | None = None
        self.cancelled = False
        self.failure_reported = False
        self.compaction_resume_activity: tuple[ActivityState, str] | None = None
        self.publication = TurnEventPublication(execution)

    @property
    def comms(self) -> Comms:
        execution = self.execution
        return execution.runner.comms

    @property
    def thread_name(self) -> str:
        execution = self.execution
        return execution.thread_name

    @handles(events.Error)
    async def error_observed(self, event: events.Error) -> None:
        self.failure_reported = True

    async def report_failure(self, error: Exception) -> None:
        """Attempt existing ACP error publication once without hiding the original fault."""
        execution = self.execution
        if self.failure_reported or execution.session_id in execution.runner.emitted_errors:
            return
        self.failure_reported = True
        try:
            await execution.runner.effects._emit_event(
                execution.session_id,
                events.Error(str(error)),
                turn_id=execution.turn_id,
                route=execution.routing.reply,
            )
        except Exception as delivery_error:
            execution.runner.effects._debug_log(
                f"turn:error-publication failed: {delivery_error!r}; original: {error!r}"
            )

    @handles(events.SteeringInterrupted)
    async def steering_interrupted(self, event: events.SteeringInterrupted) -> None:
        self.reply_parts.clear()

    @handles(events.InputRefused)
    async def input_refused(self, event: events.InputRefused) -> None:
        execution = self.execution
        await execution.runner.inputs.input_refused(execution.session_id, event.id)

    @handles(events.ProviderUsage)
    async def provider_usage(self, event: events.ProviderUsage) -> None:
        execution = self.execution
        response_id = str(event.response_id)
        usage = event.usage
        current_goal = execution.runner.comms.registry.require(execution.thread_name).goal
        current_goal_id = current_goal.id if current_goal is not None else None
        permit = self.originated_attempts.get(current_goal_id or "")
        if (
            permit is None
            and execution.goal_permit is not None
            and (current_goal_id == execution.goal_permit.reservation.goal_id)
        ):
            permit = execution.goal_permit
        if permit is not None:
            assert execution.runner.goal_store is not None
            execution.runner.goal_store.record_provider_usage(permit, response_id, usage.to_wire())
        else:
            self.unattributed_usage.append((response_id, usage))

    @handles(events.CompactionStart)
    async def compaction_started(self, event: events.CompactionStart) -> None:
        execution = self.execution
        if self.compaction_resume_activity is None:
            current_activity = execution.runner.comms.agents.activity_of(execution.thread_name)
            self.compaction_resume_activity = (
                current_activity.state,
                current_activity.detail,
            )
        execution.runner.comms.agents.set_activity(
            execution.thread_name, ActivityState.WORKING, "Compacting context"
        )

    @handles(events.CompactionEnd)
    async def compaction_ended(self, event: events.CompactionEnd) -> None:
        execution = self.execution
        if self.compaction_resume_activity is not None:
            execution.runner.comms.agents.set_activity(
                execution.thread_name, *self.compaction_resume_activity
            )
            self.compaction_resume_activity = None

    @handles(events.CompactionEvent)
    async def invalidate_context(self, event: events.CompactionEvent) -> None:
        execution = self.execution
        info = execution.runner.comms.agents.agent_info_of(execution.thread_name)
        execution.runner.comms.agents.set_agent_info(
            execution.thread_name,
            model=info.model if info else None,
            session_name=info.session_name if info else None,
            context_used=None,
            context_size=info.context_size if info else None,
        )

    @handles(events.Chunk)
    async def chunk(self, event: events.Chunk) -> None:
        execution = self.execution
        if execution.reply_targets:
            self.reply_parts.append(event.text)

    @handles(events.CommittedProgress)
    async def committed_progress(self, event: events.CommittedProgress) -> None:
        execution = self.execution
        if execution.reply_targets:
            progress = event.text
            if progress and "".join(self.reply_parts) == progress:
                # Pi committed this assistant message before tool work.
                # Publish it once as visible, non-waking progress; the
                # final reply contains only subsequent assistant text.
                for target in execution.reply_targets:
                    execution.runner.comms.messaging.send(
                        execution.thread_name, target, progress, notice=True
                    )
                self.reply_parts.clear()

    @handles(events.Done)
    async def done(self, event: events.Done) -> events.Done:
        execution = self.execution
        self.terminal_failure = asdict(event)
        unknown_attempts = not execution.runner.inputs.dispositions.read().all_started(
            execution.runner.inputs.turn_input_keys.get(execution.session_id, set())
        )
        if event.ok is True and (
            execution.runner.inputs.forwarded_inputs.get(execution.session_id) or unknown_attempts
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
        if (
            execution.goal_permit is not None
            and execution.goal is not None
            and execution.goal.state.active
            and (
                execution.runner.comms.registry.require(execution.thread_name).worktree
                == execution.thread.worktree
            )
        ):
            current_goal = execution.runner.comms.registry.require(execution.thread_name).goal
            failed = not event.ok
            # An RPC stream can settle and exit successfully after
            # accepting a user prompt without assistant output or tool
            # activity. This is not a productive goal turn.
            empty_success = (
                not failed
                and not self.successful_tool_observed
                and not str(event.text or "").strip()
            )
            if (
                current_goal
                and current_goal.id == execution.goal.id
                and current_goal.state.active
                and (failed or empty_success)
            ):
                # Block the latest same-ID goal under the wire lock,
                # retaining any newer progress from a concurrent update.
                execution.runner.comms.goals.block_goal_after_failed_turn(
                    execution.thread_name,
                    started_goal=execution.goal,
                    expected_worktree=execution.thread.worktree,
                    diagnostic=(
                        "Backend turn failed; inspect local diagnostics before resuming."
                        if failed
                        else "Backend reported success without assistant output "
                        "or tool activity; inspect the session before resuming."
                    ),
                )
        if not event.ok and event.text:
            self.failure_reported = True
        return event

    @handles(events.InputStarted)
    async def input_started(self, event: events.InputStarted) -> None:
        execution = self.execution
        await execution.runner.inputs.input_started(
            execution.session_id,
            event.id if event.id is not None else execution.accepted_input_id,
            execution.original_keys,
            execution.initial_display_text,
        )

    @handles(events.SettingChangeResult)
    async def setting_result(self, event: events.SettingChangeResult) -> None:
        execution = self.execution
        execution.runner.sessions.config.setting_requests.resolve(event)

    async def before_agent_info(self, event: events.AgentInfo) -> None:
        execution = self.execution
        session_file = event.session_file
        if (
            session_file
            and execution.runner.comms.registry.require(execution.thread_name).session_file
            != session_file
        ):
            execution.runner.comms.threads.attach_session(execution.thread_name, str(session_file))

    async def after_agent_info(self, event: events.AgentInfo) -> None:
        execution = self.execution
        await execution.runner.sessions.config.observe_agent_info(
            execution.session_id, execution.thread_name, event
        )

    @handles(events.ToolEnd)
    async def tool_ended(self, event: events.ToolEnd) -> None:
        execution = self.execution
        if event.ok:
            self.successful_tool_observed = True
        execution.thread_name = await execution.runner.sessions.sync_identity(execution.session_id)
        if event.name == "comms_set_goal" and event.ok is True:
            current_goal = execution.runner.comms.registry.require(execution.thread_name).goal
            if current_goal is not None:
                store = execution.runner.open_goal_store()
                if store.snapshot(current_goal.id) is None:
                    store.create_goal(current_goal.id)
                    self.originated_goal_ids.add(current_goal.id)
                    grant = store.ready_grant(current_goal.id, 1)
                    reservation = store.reserve(current_goal.id, 1, ready_grant=grant)
                    origin_permit = store.claim_launch(reservation)
                    self.originated_attempts[current_goal.id] = origin_permit
                    for response_id, usage in self.unattributed_usage:
                        store.record_provider_usage(origin_permit, response_id, usage.to_wire())
                    self.unattributed_usage.clear()
                    execution.runner.pending_goal_origins[execution.thread_name] = current_goal.id
        self.update_activity(ActivityState.THINKING, execution.task[:80])

    @handles(events.StreamSettled)
    async def stream_settled(self, event: events.StreamSettled) -> None:
        execution = self.execution
        self.compaction_resume_activity = None
        self.terminal_fence = execution.runner.finish_turn_stream(
            execution.session_id, execution.thread_name, execution.turn_id, execution.turn_lease
        )
        self.settled = True
        execution.finish_event.set()

    async def consume(self, event: events.AgentEvent) -> None:
        execution = self.execution
        event = await self.dispatch(event)
        await execution.runner.effects._emit_event(
            execution.session_id, event, turn_id=execution.turn_id, route=execution.routing.reply
        )
        await self.publication.dispatch(event)

    def update_activity(self, state: ActivityState, detail: str) -> None:
        execution = self.execution
        if self.compaction_resume_activity is not None:
            # Tool notifications may arrive while a summary is in flight.
            # Retain the next activity without hiding active compaction.
            self.compaction_resume_activity = (state, detail)
        else:
            execution.runner.comms.agents.set_activity(execution.thread_name, state, detail)

    async def publish_result(self):
        execution = self.execution
        if self.terminal_ok is None and execution.goal_permit is not None:
            # An EOF without a done event is a failed turn, not a signal to
            # schedule the still-active goal again on the next live drain.
            execution.current_thread = execution.runner.comms.registry.require(
                execution.thread_name
            )
            execution.current_goal = execution.current_thread.goal
            if (
                execution.current_thread.worktree == execution.thread.worktree
                and execution.current_goal is not None
                and execution.current_goal.id == execution.goal.id
                and execution.current_goal.state.active
            ):
                execution.runner.comms.goals.block_goal_after_failed_turn(
                    execution.thread_name,
                    started_goal=execution.goal,
                    expected_worktree=execution.thread.worktree,
                    diagnostic=("Backend turn ended without a result; inspect local diagnostics."),
                )
        if execution.goal_permit is not None:
            assert execution.goal is not None
            assert execution.runner.goal_store is not None
            execution.current_goal = execution.runner.comms.registry.require(
                execution.thread_name
            ).goal
            execution.verified_report = (
                execution.current_goal is not None
                and execution.current_goal.id == execution.goal.id
                and execution.current_goal.reported_turn == execution.turn_id
            )
            if (
                self.terminal_ok is True
                and execution.current_goal is not None
                and execution.current_goal.id == execution.goal.id
            ):
                execution.witness = f"native-terminal:{execution.turn_id}"
                if (
                    isinstance(execution.current_goal.state, CompletedGoal)
                    and execution.verified_report
                ):
                    execution.witness = f"registry-revision:{execution.current_goal.revision}"
                    execution.runner.goal_store.record_verified_completion(
                        execution.goal_permit, execution.witness
                    )
                    self.goal_attempt_resolved = True
                elif execution.current_goal.state.active or isinstance(
                    execution.current_goal.state, PausedGoal
                ):
                    # A successful in-flight turn may finish after owner pause.
                    # Preserve success; the scheduler will not launch while paused.
                    execution.runner.goal_store.record_verified_progress(
                        execution.goal_permit, execution.witness
                    )
                    self.goal_attempt_resolved = True
            if not self.goal_attempt_resolved:
                execution.terminal_snapshot = execution.runner.comms.registry.snapshot()
                with suppress(StaleAttemptError):
                    execution.runner.goal_store.record_failed(
                        execution.goal_permit.reservation,
                        "Goal turn ended without verified terminal progress.",
                        observation=(
                            FailedTurnObservation.from_terminal(
                                execution.goal_permit.reservation,
                                owner=execution.thread,
                                goal=execution.goal,
                                lease=execution.turn_lease,
                                turn_id=execution.turn_id,
                                admission=execution.turn_admission,
                                current_owner=execution.terminal_snapshot.threads.get(
                                    execution.thread_name
                                ),
                                current_admission=execution.terminal_snapshot.admission_generations.get(
                                    execution.thread_name
                                ),
                                reason=terminal_failure_reason(self.terminal_failure),
                            )
                            if self.terminal_ok is not True
                            else None
                        ),
                    )
                self.goal_attempt_resolved = True
                if (
                    execution.current_goal is not None
                    and execution.current_goal.id == execution.goal.id
                ):
                    execution.diagnostic = "Goal turn ended without verified terminal progress."
                    execution.runner.comms.goals.block_goal_after_failed_turn(
                        execution.thread_name,
                        started_goal=execution.goal,
                        expected_worktree=execution.thread.worktree,
                        diagnostic=execution.diagnostic,
                    )
        if execution.origins and self.settled and self.terminal_ok is True:
            await asyncio.to_thread(
                execution.runner.comms.transcripts.record_turn_routing,
                execution.thread_name,
                execution.checkpoint,
                execution.routing,
            )
        if self.terminal_ok is True:
            if self.reply_parts:
                for target in execution.reply_targets:
                    execution.runner.comms.messaging.send(
                        execution.thread_name,
                        target,
                        "".join(self.reply_parts),
                    )
        else:
            # Streamed chunks were provisional. A failed or missing terminal
            # result cannot turn them into a completed wire reply. Keep the
            # failure notice non-waking, including for human reply targets.
            # Backend text may include stderr, secrets, or content from an
            # unrelated session. Persist only structural facts for headless owners.
            execution.diagnostic_path = record_terminal_failure(
                execution.runner.comms.root,
                turn_id=execution.turn_id,
                thread=execution.thread_name,
                event=self.terminal_failure,
                sequences=tuple(origin.seq for origin in execution.origins),
            )
            execution.notice_targets = tuple(
                dict.fromkeys(
                    (*execution.reply_targets,)
                    + tuple(
                        origin.target if is_channel_target(origin.target) else origin.sender
                        for origin in execution.origins
                        if origin.sender != execution.thread_name
                    )
                )
            )
            for target in execution.notice_targets:
                execution.prefix = (
                    "Request failed" if target in execution.reply_targets else "Delivery failed"
                )
                execution.runner.comms.messaging.send(
                    execution.thread_name,
                    target,
                    f"{execution.prefix}: backend turn did not complete. "
                    f"[Open diagnostic]({execution.diagnostic_path.as_uri()})",
                    MessageType.ALERT,
                    notice=True,
                )
        await execution.runner.runtime.session_update(
            session_id=execution.session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={
                    "agentComms": {
                        "transcriptChanged": True,
                        "transcriptCursor": asdict(
                            execution.runner.comms.transcripts.transcript_checkpoint(
                                execution.thread_name
                            )
                        ),
                    }
                },
            ),
        )

    def finish_attempts(self) -> None:
        execution = self.execution
        for originated_id in self.originated_goal_ids:
            execution.current = execution.runner.comms.registry.require(execution.thread_name).goal
            execution.valid_origin = (
                self.terminal_ok is True
                and execution.current is not None
                and execution.current.id == originated_id
                and isinstance(execution.current.state, (ActiveGoal, PausedGoal, CompletedGoal))
            )
            assert execution.runner.goal_store is not None
            execution.resolved_origin_permit = self.originated_attempts.get(originated_id)
            if execution.resolved_origin_permit is not None:
                if (
                    execution.valid_origin
                    and execution.current is not None
                    and isinstance(execution.current.state, (ActiveGoal, PausedGoal))
                ):
                    execution.runner.goal_store.record_verified_progress(
                        execution.resolved_origin_permit, f"origin-final:{execution.turn_id}"
                    )
                elif (
                    execution.valid_origin
                    and execution.current is not None
                    and isinstance(execution.current.state, CompletedGoal)
                    and execution.current.reported_turn == execution.turn_id
                ):
                    execution.runner.goal_store.record_verified_completion(
                        execution.resolved_origin_permit,
                        f"origin-completed:{execution.current.revision}",
                    )
                else:
                    with suppress(StaleAttemptError):
                        execution.runner.goal_store.record_failed(
                            execution.resolved_origin_permit.reservation,
                            "Goal origin turn did not finish successfully.",
                        )
                    execution.valid_origin = False
            elif not execution.valid_origin:
                execution.generation = execution.runner.goal_store.snapshot(originated_id)
                if (
                    execution.generation is not None
                    and execution.generation.lifecycle.allows_resume(True)
                ):
                    execution.runner.goal_store.retire_goal(
                        originated_id,
                        expected_generation=execution.generation.number,
                        attempt_id=execution.generation.attempt_id,
                    )
            if (
                not execution.valid_origin
                and execution.current is not None
                and execution.current.id == originated_id
                and isinstance(execution.current.state, (ActiveGoal, CompletedGoal))
            ):
                execution.runner.comms.goals.update_goal(
                    execution.thread_name,
                    BlockedGoalAction(
                        expect=GoalPrecondition(
                            goal_id=originated_id, expected_goal=execution.current
                        ),
                        progress="Goal origin turn did not finish successfully.",
                    ),
                )
            if execution.runner.pending_goal_origins.get(execution.thread_name) == originated_id:
                execution.runner.pending_goal_origins.pop(execution.thread_name, None)
        if execution.goal_permit is not None and not self.goal_attempt_resolved:
            assert execution.runner.goal_store is not None
            with suppress(StaleAttemptError):
                execution.runner.goal_store.record_failed(
                    execution.goal_permit.reservation,
                    "Goal attempt ended without a verified terminal result.",
                )
