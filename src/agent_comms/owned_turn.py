"""One owner turn: check permission, run the native stream and record its result."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, ExitStack
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from acp import RequestError

from . import agent_events as events
from .agent_backend import InputContent, TurnDelivery
from .channel_input_batch import InputBatch
from .field_codec import Id128Text
from .goal_attempts import GoalTurnPermit, NoLaunchPermit
from .goals import Goal
from .messages import Message
from .input_attempt import ReservedInput
from .input_origin import WireInputOrigin
from .owned_send import OwnedSend
from .native_input_owner import RegistryOwner
from .native_pi import NativeRequestFailure
from .coordinator import Coordination
from .routing import MessageRoute, ScheduledTurn, TurnRouting
from .runtime import UNBOUND_CONTROLLER
from .store_files import _store_lock
from .thread_identity import TurnId
from .turn_goal_account import TurnGoalAccount
from .turn_goal_permission import (
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


class UnexpectedTurnFailure(NativeRequestFailure):
    """An execution fault is reported without claiming the native runtime is unavailable."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(f"Unexpected turn failure: {cause}")
        self.__cause__ = cause


class OwnedTurn:
    """One authorized owner turn; stable source facts and native callbacks live here."""

    @property
    def thread(self):
        return self.registry_owner.thread

    @property
    def turn_lease(self):
        return self.registry_owner.turn_lease

    @property
    def backend(self):
        return self.runner.backend_for(self.session_id, self.thread)

    @property
    def reply_targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(origin.exact_reply_target for origin in self.origins))

    async def attach_native_session(self, session_file):
        """Publish through the owner returned by this original begin operation."""
        self.registry_owner = await Coordination.run_worker(partial(
            self.runner.comms.registry.attach_native_session,
            self.registry_owner, str(session_file),
        ))

    def __init__(
        self,
        runner: TurnRunner,
        session_id: str,
        thread_name: str,
        task: str,
        *,
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
        self.origins = origins
        self.images = images
        self.original_keys = original_keys
        self.initial_display_text = initial_display_text
        self.autonomous_goal = autonomous_goal
        self.original_owner_input = original_owner_input
        self.original_goal_id = original_goal_id
        self.accepted_input_id = accepted_input_id
        self.dependency_wait_id = dependency_wait_id

    def authorize(self):
        self.original_display = (
            None if self.autonomous_goal else (self.initial_display_text or self.task)
        )
        self.registry_owner = RegistryOwner.capture_local(
            self.runner.comms.registry.snapshot(), self.thread_name
        )
        self.thread_name = self.thread.name
        wait = self.runner.comms.goals.goal_wait(self.thread_name)
        active_goal = self.thread.goal.active()
        if self.dependency_wait_id is not None:
            if not wait.matches_id(self.dependency_wait_id):
                return
        elif not wait.allows_routed_input() and not self.original_owner_input:
            return
        if self.original_owner_input and self.original_goal_id != (
            active_goal.reported_id()
        ):
            raise RequestError.invalid_params({"reason": "input_authority_changed"})
        self.goal_permit: GoalTurnPermit = NoLaunchPermit()
        return True

    def begin(self, resources: AsyncExitStack, permits: ExitStack) -> bool:
        self.turn_id = Id128Text.mint()
        self.routing = TurnRouting(
            tuple(origin.reference for origin in self.origins),
            MessageRoute(self.thread_name, self.reply_targets) if self.reply_targets else None,
        )
        self.checkpoint = self.runner.comms.transcripts.transcript_checkpoint(self.thread_name)
        self.lease_cleanup = AsyncExitStack()
        resources.push_async_callback(self.lease_cleanup.aclose)
        self.registry_owner = self.runner.acquire_turn(
            self.lease_cleanup, self.session_id, self.thread_name,
            self.turn_id, self.task[:80], self.routing,
        )
        active_goal = self.thread.goal.active()
        prepared_goal = any(self._prepare_goal_permit(goal, permits) for goal in active_goal)
        if self.autonomous_goal and not prepared_goal:
            return False
        self.input_cleanup = AsyncExitStack()
        resources.push_async_callback(self.input_cleanup.aclose)
        self.input_cleanup.push_async_callback(Coordination.run_worker, self.settle_unbound)
        self.bus_origins = tuple(origin for origin in self.origins if origin.seq > 0)
        if self.bus_origins:
            with _store_lock(self.runner.comms._wire_lock_path):
                snapshot = self.runner.comms.registry.snapshot()
                metadata = self.runner.comms.bus.log.read_metadata_unlocked()
                originals = tuple(
                    ReservedInput(
                        key=self.runner.inputs.dispositions.bus_key(origin, self.thread),
                        sequence=origin.seq,
                        owner=self.thread_name,
                        claim_scope=snapshot.claimed_generations[self.thread_name],
                        target=origin.target,
                        source_text=ScheduledTurn.incoming(
                            origin, aliases=snapshot.aliases
                        ).prompt,
                        origin=WireInputOrigin(
                            metadata.wire_root_id,
                            origin.reference,
                        ),
                    )
                    for origin in self.bus_origins
                )
                self.original_keys = (*self.original_keys, *(row.key for row in originals))
                self.runner.inputs.dispositions.record_originals(*originals)
        document = self.runner.inputs.dispositions.read(self.original_keys)
        batch = InputBatch.capture(
            self.origins,
            self.original_keys,
            self.task,
            self.thread,
            document,
        )
        self.prepare_prompt(batch)
        self.reserve_input(batch)
        return True

    def _prepare_goal_permit(self, goal: Goal, permits: ExitStack) -> bool:
        if self.original_owner_input:
            return True
        self.goal_permit = TurnGoalAccount.reserve(
            owner=self.thread, goal=goal, comms=self.runner.comms, autonomous=self.autonomous_goal,
        )
        return self.goal_permit.prepare_launch(permits, self.runner.comms.goals.open_goal_store())

    def prepare_prompt(self, batch: InputBatch):
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
            OwnerProvenance,
            RecordedContextTurn,
        )

        self.relations = self.runner.comms.relationships.context_relations(self.thread.name)
        self.context = TurnContext.for_owner(
            self.thread,
            RecordedContextTurn(TurnId(self.turn_id), self.turn_lease.identity),
            self.task,
            self.runner.comms.views.thread_views(),
            self.relations,
            self.runner.comms.goals,
            tuple(origin.reference for origin in self.origins),
            batch.originals,
        )
        self.context = self.goal_permit.add_context(self.context, self.thread, self.runner.comms.goals)
        if self.thread.auto_title_pending:
            self.context = self.context.prepend(AutomaticTitleSegment.capture(
                (OwnerProvenance(self.thread.incarnation, self.turn_id),)))
        response_targets = batch.native_reply_targets or self.reply_targets
        if response_targets:
            self.context = self.context.prepend(ReplyRouteSegment.capture(
                (OwnerProvenance(self.thread.incarnation, self.turn_id),), targets=response_targets))

    def reserve_input(self, batch: InputBatch):
        """Acquire the original durable input before opening loop capabilities."""
        if self.original_owner_input:
            permission = OwnerGoalPermission(self.thread.goal)
        else:
            permission = self.thread.goal.active().following_permission()
        if self.original_owner_input:
            source_type = OwnerOriginalInput
        elif not self.origins:
            source_type = ScheduledOriginalInput
        elif self.dependency_wait_id is not None:
            source_type = DependencyOriginalInput
        else:
            source_type = RoutedOriginalInput
        original = source_type(
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
            batch=batch,
        )
        with _store_lock(self.runner.comms._wire_lock_path), ExitStack() as reservation:
            self.original = original.reserve(
                self.runner.inputs.dispositions,
                self.thread,
                TurnId(self.turn_id),
                self.turn_lease.claimed_generation,
                ownership=reservation,
            )
            self.original_keys = self.original.keys
            # The store enlists rollback before publication. The original keys
            # now have rollback registered on the outer stack before the worker returns.
            reservation.pop_all()

    def open_stream(self, resources: AsyncExitStack, permits: ExitStack):
        self.backend_inbox = self.runner.inputs.bind_native_turn(self.session_id, self.thread)
        resources.push_async_callback(
            self.runner.inputs.finish_turn_inputs, self.session_id, self.backend_inbox
        )
        self.finish_event = asyncio.Event()
        self.controller = (
            self.runner.inputs.state(self.session_id).queued[self.accepted_input_id].controller
            if self.accepted_input_id is not None
            else self.runner.runtime.controller.get()
        )
        if self.controller is UNBOUND_CONTROLLER:
            self.controller = None  # Autonomous/channel/goal turns have no controller.
        self.runner.inputs.state(self.session_id).first_source = self.original
        self.input_cleanup.pop_all()
        origin_claims = ExitStack()
        permits.callback(origin_claims.close)
        self.progress = TurnProgress(
            comms=self.runner.comms,
            sessions=self.runner.sessions,
            inputs=self.runner.inputs,
            effects=self.runner.effects,
            runtime=self.runner.runtime,
            session_id=self.session_id,
            turn=self,
            finish_event=self.finish_event,
            goals=TurnGoalAccount(
                comms=self.runner.comms,
                owner=self.thread,
                turn=TurnId(self.turn_id),
                lease=self.turn_lease,
                permit=self.goal_permit,
                pending_origins=self.runner.goals.pending_goal_origins,
                claims=origin_claims,
            ),
            sync_goals=self.runner.goals.sync_goal_execution,
        )
        permits.callback(self.finish_goals)
        resources.push_async_callback(self.close_changed_project)

    def finish_goals(self) -> None:
        self.progress.goals.finish(self.progress.result)

    async def close_changed_project(self) -> None:
        name = await self.runner.sessions.sync_identity(self.session_id)
        current = await Coordination.run_worker(partial(self.runner.comms.registry.require, name))
        if current.worktree != self.thread.worktree:
            if persistent := self.runner.persistent_backends.get(self.session_id):
                await persistent.close_idle()

    async def prepare_native(self):
        await self.runner.effects._emit_event(
            self.session_id,
            await Coordination.run_worker(partial(
                self.runner.current_turn_update, self.session_id,
            )),
        )
        await self.runner.inputs.emit_input_delivery_changed(self.session_id)
        # This turn already holds the session authority. Publish its cursor
        # without reacquiring the observer lock held by a selected handoff;
        # Private delivery sees the registry lease and cannot start another turn.
        await self.runner.inputs.drain_owned_inbox(self.session_id)

        self.context = await Coordination.run_worker(partial(
            self.context.with_awareness, self.runner.comms, self.thread, self.relations,
        ))

    async def stream(self):
        await self.runner.transition_turn(self.session_id, self.turn_lease, PromptAcceptancePhase())
        context = await self.backend.context_for_input(self.thread, self.context)
        rendered = context.render(images=self.images)
        sender = OwnedSend(
            owner_name=self.thread.name,
            comms=self.runner.comms,
            inputs=self.runner.inputs,
            goal_store=await Coordination.run_worker(self.runner.comms.goals.open_goal_store),
            session_id=self.session_id,
            thread=self.thread,
            turn=TurnId(self.turn_id),
            claim_scope=self.turn_lease.claimed_generation,
            goal_permit=self.goal_permit,
            original=replace(self.original, prompt=rendered.text),
        )
        from .channel_coding_tools import CodingToolMode

        delivery = TurnDelivery(
            tool_mode=CodingToolMode(sender),
            worktree=self.worktree,
            environment=self.env_extra,
            inbox=self.backend_inbox,
            finish_event=self.finish_event,
            send_boundary=sender,
            interrupt_boundary=lambda public_id, native_id, text: sender(
                public_id, native_id, text, already_bound=True
            ),
            input_started=sender.native_start,
            request_progress=self.progress.record_request_progress,
            ui_request=lambda request: self.runner.extension_ui_permission(
                self.session_id, self.turn_id, self.controller, request
            ),
        )
        content = InputContent(
            text=rendered.text, images=tuple(self.images), contributions=rendered.contributions,
        )
        async for event in self.backend.run_turn(self.thread, content, delivery):
            await self.progress.consume(event)

    async def acquire(self, resources: AsyncExitStack, permit_cleanup: AsyncExitStack) -> bool:
        """Join source acquisition before exposing this turn's loop capabilities."""
        self.owner_task = asyncio.current_task()
        assert self.owner_task is not None
        permits = ExitStack()
        permit_cleanup.push_async_callback(Coordination.run_worker, permits.close)
        if not await Coordination.run_worker(self.authorize):
            return False
        self.runner.sessions.bindings[self.session_id] = self.thread_name
        if not await Coordination.run_worker(partial(self.begin, resources, permits)):
            return False
        self.open_stream(resources, permits)
        # Early failures close claims before inbox/lease retirement. Once all
        # stream capabilities exist, goal settlement belongs above those resources.
        resources.push_async_callback(permit_cleanup.pop_all().aclose)
        resources.push_async_callback(self.backend.abort, self.owner_task)
        return True

    async def run(self) -> None:
        async with AsyncExitStack() as resources:
            async with AsyncExitStack() as permit_cleanup:
                if not await self.acquire(resources, permit_cleanup):
                    return
                await self.run_native()

    def settle_unbound(self):
        """Close the actual input batch and observe its result under one wire cut."""
        with _store_lock(self.runner.comms._wire_lock_path):
            document = self.runner.inputs.dispositions.settle_unbound(self.original_keys)
            return document.shared_state(self.original_keys)

    async def fail(self, failure: Exception, cause: Exception) -> NoReturn:
        """Settle and report once, retaining the exception that actually failed the turn."""
        await Coordination.run_worker(self.settle_unbound)
        outcome = await self.progress.report_failure(failure)
        outcome.raise_fault(cause)

    async def run_native(self) -> None:
        try:
            await self.prepare_native()
            await self.stream()
        except asyncio.CancelledError:
            await self.runner.transition_turn(self.session_id, self.turn_lease, CancellingPhase())
            await self.backend.abort(self.owner_task)
            state = await Coordination.run_worker(self.settle_unbound)
            await self.runner.effects._emit_event(
                self.session_id, events.PromptCancelled(state), turn_id=self.turn_id
            )
            raise
        except (NativeRequestFailure, RequestError) as error:
            await self.fail(error, error)
        except Exception as error:
            await self.fail(UnexpectedTurnFailure(error), error)
        # The completed outcome owns notification delivery and request failure;
        # it is outside stream-fault reporting, so no failure is published twice.
        await self.progress.publish_result()
