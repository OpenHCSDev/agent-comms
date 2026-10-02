"""Single owner-turn admission, native stream and terminal settlement."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, ExitStack
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from acp import RequestError

from . import agent_events as events
from . import backend
from .acp_failure import PromptFailureReceipt
from .channel_input_batch import InputBatch
from .goal_attempts import LaunchPermit
from .messages import Message
from .input_origin import WireInputOrigin
from .owned_send_admission import OwnedSendAdmission
from .routing import MessageRoute, ScheduledTurn, TurnRouting
from .runtime import UNBOUND_CONTROLLER
from .selected_summary_admission import SelectedSummaryAdmission
from .store_files import _store_lock
from .thread_identity import TurnId
from .turn_goal_account import TurnGoalAccount
from .turn_goal_permission import (
    ContinuationGoalPermission,
    InactiveGoalPermission,
    OwnerGoalPermission,
)
from .turn_input_source import (
    CapturedInputDependency,
    DependencyOriginalInput,
    NoInputDependency,
    OwnerOriginalInput,
    RoutedOriginalInput,
    ScheduledOriginalInput,
)
from .turn_progress import TurnProgress
from .turn_phase import CancellingPhase, PromptAcceptancePhase

if TYPE_CHECKING:
    from .turn_runner import TurnRunner


class OwnedTurn:
    """One admitted owner turn; stable source facts and native callbacks live here."""

    def __init__(
        self,
        runner: TurnRunner,
        session_id: str,
        thread_name: str,
        task: str,
        *,
        reply_targets: tuple[str, ...] = (),
        origins: tuple[Message, ...] = (),
        images: tuple[Any, ...] = (),
        original_keys: tuple[str, ...] = (),
        initial_display_text: str | None = None,
        autonomous_goal: bool = False,
        original_owner_input: bool = False,
        original_goal_id: str | None = None,
        accepted_input_id: str | None = None,
        dependency_wait_id: str | None = None,
    ) -> None:
        self.runner = runner
        self.session_id = session_id
        self.thread_name = thread_name
        self.task = task
        self.reply_targets = reply_targets
        self.origins = origins
        self.images = images
        self.original_keys = original_keys
        self.initial_display_text = initial_display_text
        self.autonomous_goal = autonomous_goal
        self.original_owner_input = original_owner_input
        self.original_goal_id = original_goal_id
        self.accepted_input_id = accepted_input_id
        self.dependency_wait_id = dependency_wait_id

    def admit(self, permits: ExitStack):
        self.original_display = (
            None if self.autonomous_goal else (self.initial_display_text or self.task)
        )
        self.owner_task = asyncio.current_task()
        assert self.owner_task is not None
        self.thread = self.runner.comms.registry.require(self.thread_name)
        self.thread_name = self.thread.name
        wait = self.runner.comms.goals.goal_wait(self.thread_name)
        active_goal = self.thread.active_goal
        if (
            wait is not None
            and not self.original_owner_input
            and wait.wait_id != self.dependency_wait_id
        ):
            return
        if self.dependency_wait_id is not None and (
            wait is None or wait.wait_id != self.dependency_wait_id
        ):
            return
        if self.original_owner_input and self.original_goal_id != (
            active_goal.id if active_goal is not None else None
        ):
            raise RequestError.invalid_params({"reason": "input_authority_changed"})
        self.goal_permit: LaunchPermit | None = None
        if self.autonomous_goal and active_goal is None:
            return
        if active_goal is not None and not self.original_owner_input:
            self.goal_permit = TurnGoalAccount.reserve(
                owner=self.thread,
                comms=self.runner.comms,
                store=self.runner.goals.goal_store,
                open_store=self.runner.goals.open_goal_store,
                ready_grant=self.runner.goals.ready_goal_grant_locked,
                autonomous=self.autonomous_goal,
            )
            if self.goal_permit is None:
                return
            permits.callback(
                self.goal_permit.retire_unverified, self.runner.goals.goal_store
            )
        self.runner.sessions.bindings[self.session_id] = self.thread_name
        return True

    def begin(self, resources: AsyncExitStack):
        self.runner.emitted_errors.pop(self.session_id, None)
        self.turn_id = uuid4().hex
        self.routing = TurnRouting(
            tuple(origin.reference for origin in self.origins),
            MessageRoute(self.thread_name, self.reply_targets) if self.reply_targets else None,
        )
        self.checkpoint = self.runner.comms.transcripts.transcript_checkpoint(self.thread_name)
        self.turn_lease = self.runner.comms.agents.begin_turn(
            self.thread_name, self.turn_id, self.task[:80], self.routing
        )
        self.lease_custody = AsyncExitStack()
        self.lease_custody.push_async_callback(
            self.runner.settle_turn,
            self.session_id, self.thread_name, self.turn_id, self.turn_lease,
        )
        resources.push_async_callback(self.lease_custody.aclose)
        self.bus_origins = tuple(origin for origin in self.origins if origin.seq > 0)
        if self.bus_origins:
            with _store_lock(self.runner.comms._wire_lock_path):
                self.snapshot = self.runner.comms.registry.snapshot()
                for origin in self.bus_origins:
                    self.key = self.runner.inputs.dispositions.bus_key(origin, self.thread)
                    if self.runner.inputs.dispositions.read().rows.get(self.key) is None:
                        self.runner.inputs.dispositions.record(
                            self.key,
                            seq=origin.seq,
                            owner=self.thread_name,
                            admission=self.snapshot.admission_generations[self.thread_name],
                            target=origin.target,
                            text=ScheduledTurn.incoming(
                                origin, aliases=self.snapshot.aliases
                            ).prompt,
                            origin=WireInputOrigin(
                                self.runner.comms.bus.log.read_metadata_unlocked().wire_root_id,
                                origin.reference,
                            ),
                        )
                    self.original_keys = (*self.original_keys, self.key)
        self.batch = InputBatch.capture(
            self.origins,
            self.original_keys,
            self.task,
            self.thread,
            self.runner.inputs.dispositions,
        )

    def prepare_prompt(self):
        self.worktree = (
            self.thread.worktree if Path(self.thread.worktree).is_dir() else str(Path.cwd())
        )
        self.env_extra = self.thread.native_environment(
            self.runner.comms.root, self.runner.comms.registry.snapshot(), self.worktree
        )
        from .turn_context import (
            TurnContext,
            AutomaticTitleSegment,
            ReplyRouteSegment,
            InstructionFile,
            OwnerProvenance,
            RecordedContextTurn,
        )

        input_rows = self.runner.inputs.dispositions.read().rows
        self.context = TurnContext.for_owner(
            self.thread,
            RecordedContextTurn(TurnId(self.turn_id), self.turn_lease.identity),
            self.task,
            self.runner.comms.views.thread_views(),
            tuple(origin.reference for origin in self.origins),
            tuple(input_rows[key] for key in self.original_keys),
        )
        if self.goal_permit is not None:
            for segment in self.thread.context_goal_segments():
                self.context = self.context.prepend(segment)
        if self.thread.auto_title_pending:
            instruction = InstructionFile.read("automatic-title.md")
            self.context = self.context.prepend(
                AutomaticTitleSegment(
                    provenance=(
                        instruction.source,
                        OwnerProvenance(self.thread.incarnation, self.turn_id),
                    ),
                    instruction=instruction,
                )
            )
        if self.reply_targets:
            instruction = InstructionFile.read("reply-route.md")
            self.context = self.context.prepend(
                ReplyRouteSegment(
                    provenance=(
                        instruction.source,
                        OwnerProvenance(self.thread.incarnation, self.turn_id),
                    ),
                    instruction=instruction,
                    targets=self.reply_targets,
                )
            )

    def open_stream(self, resources: AsyncExitStack, permits: ExitStack):
        self.backend_inbox = self.runner.inputs.bind_native_turn(
            self.session_id, self.thread, self.turn_lease.admission_generation, self.turn_id
        )
        resources.push_async_callback(
            self.runner.inputs.finish_turn_inputs, self.session_id, self.backend_inbox
        )
        self.finish_event = asyncio.Event()
        self.controller = (
            self.runner.inputs.queued_inputs[self.session_id][self.accepted_input_id].controller
            if self.accepted_input_id is not None
            else self.runner.runtime.controller.get()
        )
        if self.controller is UNBOUND_CONTROLLER:
            self.controller = None  # Autonomous/channel/goal turns have no controller.
        if self.original_owner_input:
            permission = OwnerGoalPermission(self.thread.goal)
        elif active_goal := self.thread.active_goal:
            permission = ContinuationGoalPermission(active_goal)
        else:
            permission = InactiveGoalPermission()
        if self.original_owner_input:
            source_type = OwnerOriginalInput
        elif not self.origins:
            source_type = ScheduledOriginalInput
        elif self.dependency_wait_id is not None:
            source_type = DependencyOriginalInput
        else:
            source_type = RoutedOriginalInput
        self.original = source_type(
            accepted_id=self.accepted_input_id,
            goal_permission=permission,
            prompt=self.context.render().text,
            original_display=self.original_display,
            origins=self.origins,
            dependency=(
                CapturedInputDependency(self.dependency_wait_id)
                if self.dependency_wait_id is not None
                else NoInputDependency()
            ),
            batch=self.batch,
        ).reserve(
            self.runner.inputs.dispositions,
            self.thread,
            TurnId(self.turn_id),
            self.turn_lease.admission_generation,
        )
        self.original_keys = self.original.keys
        self.runner.inputs.turn_input_keys.setdefault(self.session_id, set()).update(
            self.original_keys
        )
        self.runner.inputs.original_sources[self.session_id] = self.original
        origin_claims = ExitStack()
        permits.callback(origin_claims.close)
        self.progress = TurnProgress(
            comms=self.runner.comms,
            sessions=self.runner.sessions,
            inputs=self.runner.inputs,
            effects=self.runner.effects,
            runtime=self.runner.runtime,
            emitted_errors=self.runner.emitted_errors,
            session_id=self.session_id,
            thread=self.thread,
            turn_lease=self.turn_lease,
            routing=self.routing,
            original=self.original,
            checkpoint=self.checkpoint,
            finish_event=self.finish_event,
            goals=TurnGoalAccount(
                comms=self.runner.comms,
                owner=self.thread,
                turn=TurnId(self.turn_id),
                lease=self.turn_lease,
                permit=self.goal_permit,
                open_store=self.runner.goals.open_goal_store,
                pending_origins=self.runner.goals.pending_goal_origins,
                claims=origin_claims,
            ),
            sync_goals=self.runner.goals.sync_goal_execution,
        )
        permits.callback(self.finish_goals)
        resources.push_async_callback(self.close_changed_project)
        self.lease_custody.callback(self.continue_changed_project)

    def finish_goals(self) -> None:
        self.progress.goals.finish(self.progress.result)

    async def close_changed_project(self) -> None:
        name = await self.runner.sessions.sync_identity(self.session_id)
        if self.runner.comms.registry.require(name).worktree != self.thread.worktree:
            if persistent := self.runner.persistent_backends.get(self.session_id):
                await persistent.close_idle()

    def continue_changed_project(self) -> None:
        if self.progress.result is not None:
            name = self.runner.sessions.bindings[self.session_id]
            self.runner.inputs.continue_in_project(
                self.session_id, self.progress.result, self.thread.worktree,
                self.runner.comms.registry.require(name).worktree,
            )

    async def prepare_native(self):
        await self.runner.effects._emit_event(
            self.session_id,
            self.runner.current_turn_update(self.session_id),
        )
        await self.runner.inputs.emit_input_delivery_changed(self.session_id)
        # This turn already holds the session authority. Publish its cursor
        # without reacquiring the observer lock held by a selected handoff;
        # Private delivery sees the registry lease and cannot start another turn.
        await self.runner.inputs.drain_owned_inbox(self.session_id)

        # Existing local ACP owner session only. If delivery is uncertain,
        # the keyed metadata remains pending; never invent a bus recipient.
        await self.runner.effects.publish_pending_compaction(self.session_id, self.thread_name)
        for segment in self.runner.comms.bus.awareness_segments(self.thread):
            self.context = self.context.append(segment)
        pending_keys = self.original.compaction_keys(self.thread.session_file)
        if pending_keys:
            from .owner_compaction_adaptive import maybe_compact_owner_turn

            def admit_original(admission: SelectedSummaryAdmission) -> None:
                self.runner.inputs.selected_summary_admissions[self.session_id] = admission

            try:
                prepared = await self.runner.prepare_selected_session(self.session_id, self.thread)
                self.committed = await maybe_compact_owner_turn(
                    self.runner.comms.registry,
                    self.thread_name,
                    self.turn_id,
                    prepared,
                    pending_keys,
                    self.runner.persistent_backends.setdefault(
                        self.session_id, backend.PersistentPiSession()
                    ),
                    input_text=self.context.render().text,
                    on_admission=admit_original,
                    future_queue=self.runner.inputs,
                    on_event=self.progress.consume,
                )
            except Exception:
                # A selected adaptive operation may already have paid or
                # written. Preserve the failed original input outcome.
                if self.goal_permit is not None:
                    self.runner.comms.goals.block_goal_after_failed_turn(
                        self.thread_name,
                        started_goal=self.thread.goal,
                        expected_worktree=self.thread.worktree,
                        diagnostic=(
                            "Adaptive native compaction did not establish a "
                            "safe outcome; inspect the exact commit journal."
                        ),
                    )
                raise
            if self.committed:
                # Local metadata-only outbox; uncertain subscriber delivery
                # leaves its exact row pending, never broadcasts a summary.
                await self.runner.effects.publish_pending_compaction(
                    self.session_id, self.thread_name
                )
        self.image_options: dict[str, Any] = {"images": self.images} if self.images else {}

    async def stream(self):
        await self.runner.transition_turn(self.session_id, self.turn_lease, PromptAcceptancePhase())
        rendered = self.context.render(images=self.images)
        admission = OwnedSendAdmission(
            comms=self.runner.comms,
            inputs=self.runner.inputs,
            goal_store=self.runner.goals.goal_store,
            session_id=self.session_id,
            thread=self.thread,
            turn=TurnId(self.turn_id),
            admission=self.turn_lease.admission_generation,
            goal_permit=self.goal_permit,
            original=replace(self.original, prompt=rendered.text),
        )
        async for event in backend.stream_agent_events(
            self.runner.agent_bin,
            self.runner.native_arguments(self.thread),
            rendered.text,
            self.worktree,
            self.env_extra,
            **self.image_options,
            context_contributions=rendered.contributions,
            session_file=self.thread.session_file,
            steering_queue=self.backend_inbox,
            finish_event=self.finish_event,
            send_boundary=admission,
            interrupt_boundary=lambda public_id, native_id, text: admission(
                public_id, native_id, text, already_bound=True
            ),
            native_start=admission.native_start,
            persistent_session=self.runner.persistent_backends.setdefault(
                self.session_id, backend.PersistentPiSession()
            ),
            ui_request=lambda request: self.runner.extension_ui_permission(
                self.session_id, self.turn_id, self.controller, request
            ),
        ):
            await self.progress.consume(event)

    async def run(self) -> None:
        async with AsyncExitStack() as resources:
            with ExitStack() as permits:
                if not self.admit(permits):
                    return
                self.begin(resources)
                self.prepare_prompt()
                self.open_stream(resources, permits)
                # Early failures close the local claim scope. Once every stream
                # capability is acquired, transfer its callbacks above the inbox
                # and lease, so goal settlement precedes their retirement.
                resources.enter_context(permits.pop_all())
                resources.push_async_callback(backend.terminate_task_process, self.owner_task)
                await self.run_native()

    async def run_native(self) -> None:
        try:
            await self.prepare_native()
            await self.stream()
            await self.progress.publish_result()
            if failure := self.runner.emitted_errors.get(self.session_id):
                raise PromptFailureReceipt(failure, True).request_error()
        except asyncio.CancelledError:
            await self.runner.transition_turn(self.session_id, self.turn_lease, CancellingPhase())
            await backend.terminate_task_process(self.owner_task)
            with _store_lock(self.runner.comms._wire_lock_path):
                self.runner.inputs.dispositions.settle_unbound(self.original_keys)
                state = self.runner.inputs.dispositions.read().shared_state(self.original_keys)
            await self.runner.effects._emit_event(
                self.session_id, events.PromptCancelled(state), turn_id=self.turn_id
            )
            raise
        except Exception as error:
            with _store_lock(self.runner.comms._wire_lock_path):
                self.runner.inputs.dispositions.settle_unbound(self.original_keys)
            await self.progress.report_failure(error)
            failure = self.runner.emitted_errors.get(self.session_id)
            if failure is not None:
                raise PromptFailureReceipt(failure, True).request_error() from error
            raise
