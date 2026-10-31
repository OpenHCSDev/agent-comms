"""One owner turn: check permission, run the native stream and record its result."""

from __future__ import annotations

import asyncio
from collections.abc import (
    Awaitable,
    Callable,
)
from contextlib import AsyncExitStack, ExitStack, aclosing
from dataclasses import (
    dataclass,
    replace,
)
from functools import partial
from typing import TYPE_CHECKING, NoReturn

from agent_comms.following_inputs import OwnerFollowingInputs
from agent_comms.goal_turn_permit import GoalTurnPermit, NoLaunchPermit

from . import agent_events as events
from .agent_backend import InputStartListener
from .turn_inbox import InputContent, TurnInbox
from .channel_coding_tools import CodingToolMode
from .channel_input_batch import InputBatch
from .coordination_errors import CoordinationError, Refused
from .coordinator import Coordination
from .cursor_publication import CursorDelivery
from .field_codec import AcpSessionId, FieldCodec, GoalId, TurnId
from .goals import Goal
from .image_input import ImageInput
from .input_attempt import ReservedInput
from .input_key import InputKey
from .input_origin import WireInputOrigin
from .messages import Message
from .native_failures import NativeRequestFailure
from .native_input_owner import RegistryOwner
from .native_tool_call import NativeToolMode
from .owned_send import OwnedSend
from .pi_events import (
    DialogUiRequest,
    ExtensionUiChoice,
)
from .routing import MessageRoute, NoMessageRoute, ScheduledTurn, TurnRouting
from .send_outcome import SendBoundary
from .transcript_routes import DisplayText, NoDisplayText
from .turn_failure import BackendFailed, CoordinationRefused, TurnFailure
from .turn_goal_permission import (
    OwnerGoalPermission,
    TurnGoalPermission,
)
from .turn_input_source import (
    InputDependency,
    NoInputDependency,
    OwnerOriginalInput,
    ScheduledOriginalInput,
    TurnAcceptance,
)
from .turn_phase import CancellingPhase, PromptAcceptancePhase
from .turn_progress import TurnProgress
from .turn_terminal import EndedWithoutResult, TurnTerminal

from agent_comms.errors import GoalAttemptError
from dataclasses import field
from .turn_item import UsageChargeId
if TYPE_CHECKING:
    from .turn_runner import TurnRunner
    from .goal_attempts import LaunchPermit
    from .comms import Comms
    from agent_comms.pi_wire import PiUsage
    from .threads import Thread
    from .turn_lease import TurnLeaseFence


@dataclass(kw_only=True)
class TurnGoalAccount:
    comms: Comms
    owner: Thread
    turn: TurnId
    lease: TurnLeaseFence
    permit: GoalTurnPermit
    pending_origins: dict[str, GoalId]
    claims: ExitStack
    originated: dict[GoalId, GoalTurnPermit] = field(default_factory=dict, init=False)
    unattributed_usage: list[tuple[UsageChargeId, PiUsage]] = field(default_factory=list, init=False)
    has_productive_tool: bool = field(default=False, init=False)

    @staticmethod
    def reserve(*, owner: Thread, goal: Goal, comms: Comms, autonomous: bool) -> GoalTurnPermit:
        try:
            store = comms.goals.open_goal_store()
            with comms.registry.store.current_snapshot() as cut:
                claim_scope = cut.snapshot.claim_generation(owner.name)
                generation = store.require_generation(goal.id)
                return generation.lifecycle.turn_permit(
                    store, comms.goals, owner, claim_scope, generation
                )
        except GoalAttemptError as error:
            if autonomous:
                return NoLaunchPermit()
            raise Refused.invalid_params(
                {
                    "reason": "goal_attempt_unavailable",
                    "details": "The goal has no launchable attempt. Inspect its state and use "
                    "Retry for a failed attempt; no prompt was sent to Pi.",
                }
            ) from error

    @property
    def thread_name(self) -> str:
        snapshot = self.comms.registry.snapshot()
        return snapshot.canonical_name(self.owner.name)

    def provider_usage(self, event: events.ProviderUsage) -> None:
        for goal in self.comms.registry.require(self.thread_name).goal:
            permit = self.originated.get(goal.id, self.permit)
            if permit.charge_for(goal.id, self.comms.goals, event.response_id, event.usage):
                return
        self.unattributed_usage.append((event.response_id, event.usage))

    def tool_ended(self, event: events.ToolEnd) -> None:
        self.has_productive_tool |= bool(event.ok)
        if event.name != "comms_set_goal" or event.ok is not True:
            return
        with self.comms.registry.store.current_snapshot() as cut:
            owner = cut.snapshot.require(self.owner.name)
            claimed = tuple(permit for goal in owner.goal for permit in self._reserve_origin(owner, goal))
        for permit in claimed:
            self._record_origin_usage(permit)

    def _reserve_origin(self, owner: Thread, goal: Goal) -> tuple[LaunchPermit, ...]:
        if goal.id in self.originated:
            return ()
        store = self.comms.goals.open_goal_store()
        self.originated[goal.id] = NoLaunchPermit()
        generation = store.require_generation(goal.id)
        generation.lifecycle.require_ready()
        grant = self.comms.goals.ready_goal_grant_locked(
            owner, self.lease.claimed_generation, generation,
        )
        permit = store.claim_launch(
            store.reserve(goal.id, generation.number, ready_grant=grant)
        )
        self.claims.callback(permit.retire_unverified, store)
        self.originated[goal.id] = permit
        return (permit,)

    def _record_origin_usage(self, permit: LaunchPermit) -> None:
        store = self.comms.goals.open_goal_store()
        for response_id, usage in self.unattributed_usage:
            permit.record_provider_usage(store, response_id, usage)
        self.unattributed_usage.clear()
        self.pending_origins[self.thread_name] = permit.reservation.goal_id

    def settle_launched(self, permit: LaunchPermit, terminal: TurnTerminal) -> None:
        store = self.comms.goals.open_goal_store()
        captured = self.owner.goal.require().require_id(permit.reservation.goal_id)
        current = captured.continued_by(self.comms.registry.require(self.thread_name).goal)
        terminal.settle_launched(self, permit, store, captured, current)

    def finish(self, terminal: TurnTerminal) -> None:
        """Settle all original grants once from the actual producer terminal event."""
        terminal.settle_current(self)
        self.permit.settle(self, terminal)
        for goal_id, permit in self.originated.items():
            current = self.comms.registry.require(self.thread_name).goal.matching(goal_id)
            store = self.comms.goals.open_goal_store()
            terminal.settle_origin(self, permit, store, goal_id, current)
            if self.pending_origins.get(self.thread_name) == goal_id:
                self.pending_origins.pop(self.thread_name, None)


@dataclass(frozen=True, kw_only=True)
class TurnDelivery:
    """Core's bindings for one turn, handed to the backend that runs it.

    ``inbox`` carries :class:`InputRequest` and :class:`SendNow` records that
    arrive while the turn runs. Core checks input permission at these boundaries: the
    backend asks ``send_boundary`` before writing each input or interrupt;
    ``input_start`` reports that the
    runtime began an input; ``request_progress`` and ``ui_request`` observe
    provider progress and answer runtime dialogs.
    """

    worktree: str
    environment: dict[str, str]
    inbox: TurnInbox
    finish_event: asyncio.Event
    send_boundary: SendBoundary
    input_start: InputStartListener
    cursor_delivery: CursorDelivery
    request_progress: Callable[..., None]
    ui_request: Callable[[DialogUiRequest], Awaitable[ExtensionUiChoice]]
    tool_mode: NativeToolMode


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
        session_id: AcpSessionId,
        thread_name: str,
        task: str,
        *,
        origins: tuple[Message, ...] = (),
        images: tuple[ImageInput, ...] = (),
        original_keys: tuple[InputKey, ...] = (),
        initial_display_text: str | None = None,
        autonomous_goal: bool = False,
        original_owner_input: bool = False,
        original_goal_id: GoalId | None = None,
        acceptance: TurnAcceptance,
        dependency: InputDependency = NoInputDependency(),
    ) -> None:
        self.runner = runner
        self.session_id = FieldCodec.decode(AcpSessionId, session_id)
        self.thread_name = thread_name
        self.task = task
        self.origins = origins
        self.images = images
        self.original_keys = original_keys
        self.initial_display_text = initial_display_text
        self.autonomous_goal = autonomous_goal
        self.original_owner_input = original_owner_input
        self.original_goal_id = original_goal_id
        self.acceptance = acceptance
        self.dependency = dependency
        self.terminal: TurnTerminal = EndedWithoutResult()

    def authorize(self):
        self.original_display = (
            NoDisplayText() if self.autonomous_goal else DisplayText(self.initial_display_text or self.task)
        )
        self.registry_owner = RegistryOwner.capture_local(
            self.runner.comms.registry.snapshot(), self.thread_name
        )
        self.thread_name = self.thread.name
        wait = self.runner.comms.goals.goal_wait(self.thread_name)
        active_goal = self.thread.goal.active()
        if not self.dependency.authorizes(wait, self.original_owner_input):
            return
        if self.original_owner_input and self.original_goal_id != (
            active_goal.reported_id()
        ):
            raise Refused.invalid_params({"reason": "input_authority_changed"})
        self.goal_permit: GoalTurnPermit = NoLaunchPermit()
        return True

    def begin(self, resources: AsyncExitStack, permits: ExitStack) -> bool:
        self.turn_id = TurnId.mint()
        self.routing = TurnRouting(
            tuple(origin.reference for origin in self.origins),
            MessageRoute(self.thread_name, self.reply_targets) if self.reply_targets else NoMessageRoute(),
        )
        self.checkpoint = self.runner.comms.transcripts.transcript_checkpoint(self.thread_name)
        self.input_cleanup = AsyncExitStack()
        resources.push_async_callback(self.input_cleanup.aclose)
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
        origin_claims = ExitStack()
        permits.callback(origin_claims.close)
        self.goals = TurnGoalAccount(
            comms=self.runner.comms, owner=self.thread, turn=self.turn_id,
            lease=self.turn_lease, permit=self.goal_permit,
            pending_origins=self.runner.goals.pending_goal_origins, claims=origin_claims,
        )
        permits.callback(self.finish_goals)
        self.input_cleanup.push_async_callback(Coordination.run_worker, self.settle_unbound)
        self.bus_origins = tuple(origin for origin in self.origins if origin.seq > 0)
        if self.bus_origins:
            with self.runner.comms.registry.store.current_snapshot() as cut:
                privacy, snapshot = cut.privacy, cut.snapshot
                originals = tuple(
                    ReservedInput(
                        key=self.runner.inputs.dispositions.bus_key(origin, self.thread),
                        sequence=origin.seq,
                        owner=self.thread_name,
                        claim_generation=snapshot.claim_generation(self.thread_name),
                        target=origin.target,
                        source_text=ScheduledTurn.incoming(
                            origin, aliases=snapshot.aliases
                        ).prompt,
                        origin=WireInputOrigin(
                            privacy.namespace,
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
        self.env_extra = self.runner.inputs.state(self.session_id).owned().configuration.native_environment(
            self.thread, self.runner.comms.registry.snapshot()
        )
        from .turn_context import (
            AutomaticTitleSegment,
            RecordedContextTurn,
            ReplyRouteSegment,
            TurnContext,
            TurnProvenance,
        )

        self.relations = self.runner.comms.relationships.context_relations(self.thread.name)
        self.context = TurnContext.for_owner(
            self.thread,
            RecordedContextTurn(self.turn_id, self.turn_lease.identity),
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
                (TurnProvenance(self.thread.incarnation, self.turn_id),)))
        response_targets = batch.native_reply_targets or self.reply_targets
        if response_targets:
            self.context = self.context.prepend(ReplyRouteSegment.capture(
                (TurnProvenance(self.thread.incarnation, self.turn_id),), targets=response_targets))

    def reserve_input(self, batch: InputBatch):
        """Acquire the original durable input before opening loop capabilities."""
        goal = self.runner.comms.registry.require(self.thread_name).goal
        if self.original_owner_input:
            permission = OwnerGoalPermission(goal.authority)
        else:
            permission = goal.active().following_permission()
        if self.original_owner_input:
            source_type = OwnerOriginalInput
        elif not self.origins:
            source_type = ScheduledOriginalInput
        else:
            source_type = self.dependency.original_type()
        original = source_type(
            acceptance=self.acceptance,
            goal_permission=TurnGoalPermission(permission),
            prompt=self.context.render().text,
            original_display=self.original_display,
            origins=self.origins,
            dependency=self.dependency,
            batch=batch,
        )
        with ExitStack() as reservation:
            self.original = original.reserve(
                self.runner.inputs.dispositions,
                self.thread,
                self.turn_id,
                self.turn_lease.claimed_generation,
                ownership=reservation,
            )
            self.original_keys = self.original.keys
            # The store enlists rollback before publication. The original keys
            # now have rollback registered on the outer stack before the worker returns.
            reservation.pop_all()

    def open_stream(self, resources: AsyncExitStack, permits: ExitStack):
        self.following = OwnerFollowingInputs(
            self.runner.inputs, self.session_id, self.thread,
            self.runner.inputs.bind_native_turn(self.session_id, self.thread),
        )
        self.finish_event = asyncio.Event()
        self.controller = self.acceptance.controller(
            self.runner.inputs.state(self.session_id), self.runner.runtime,
        )
        if self.controller is not None:
            self.controller = self.controller.attached(None)  # Autonomous/channel/goal turns have no controller.
        self.runner.inputs.state(self.session_id).capture_first_source(self.original)
        self.input_cleanup.pop_all()
        self.input_cleanup.push_async_callback(
            self.runner.inputs.finish_turn_inputs, self.following
        )
        self.progress = TurnProgress(
            comms=self.runner.comms,
            sessions=self.runner.sessions,
            inputs=self.runner.inputs,
            effects=self.runner.effects,
            runtime=self.runner.runtime,
            session_id=self.session_id,
            turn=self,
            finish_event=self.finish_event,
            goals=self.goals,
            sync_goals=self.runner.goals.sync_goal_execution,
        )
        resources.push_async_callback(self.close_changed_project)

    def finish_goals(self) -> None:
        self.goals.finish(self.runner.inputs.state(self.session_id).owned().stop.interrupting(self.terminal))

    async def close_changed_project(self) -> None:
        name = await self.runner.sessions.sync_identity(self.session_id)
        current = await Coordination.run_worker(partial(self.runner.comms.registry.require, name))
        if current.worktree != self.thread.worktree:
            if persistent := self.runner.persistent_backends.get(self.session_id):
                await persistent.close_idle()

    async def prepare_native(self):
        await self.progress.consumer.dispatch(await Coordination.run_worker(partial(
            self.runner.current_turn_update, self.session_id,
        )))
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
            comms=self.runner.comms,
            inputs=self.runner.inputs,
            goal_store=await Coordination.run_worker(self.runner.comms.goals.open_goal_store),
            session_id=self.session_id,
            thread=self.thread,
            turn=self.turn_id,
            claim_generation=self.turn_lease.claimed_generation,
            goal_permit=self.goal_permit,
            original_source=replace(self.original, prompt=rendered.text),
        )

        delivery = TurnDelivery(
            tool_mode=CodingToolMode(sender),
            worktree=str(self.thread.project_path),
            environment=self.env_extra,
            inbox=self.following.inbox,
            finish_event=self.finish_event,
            send_boundary=sender,
            input_start=sender,
            cursor_delivery=self.runner.inputs.effects.cursors.delivery(self.session_id),
            request_progress=self.progress.record_request_progress,
            ui_request=lambda request: self.runner.extension_ui_permission(
                self.session_id, self.turn_id, self.controller, request
            ),
        )
        content = InputContent(
            text=rendered.text, images=tuple(self.images), contributions=rendered.contributions,
        )
        try:
            async with aclosing(self.backend.run_turn(self.thread, content, delivery)) as stream:
                async for event in stream:
                    await self.progress.consume(event)
        finally:
            await sender.release_resources()

    async def acquire(self, resources: AsyncExitStack, permit_cleanup: AsyncExitStack) -> bool:
        """Join source acquisition before exposing this turn's loop capabilities."""
        self.owner_task = asyncio.current_task()
        assert self.owner_task is not None
        permits = ExitStack()
        permit_cleanup.push_async_callback(Coordination.run_worker, permits.close)
        if not await Coordination.run_worker(self.authorize):
            return False
        await self.runner.sessions.sync_identity(self.session_id)
        if not await Coordination.run_worker(partial(self.begin, resources, permits)):
            return False
        self.open_stream(resources, permits)
        # Early failures close claims before inbox/lease retirement. Once all
        # stream capabilities exist, goal settlement belongs above those resources.
        resources.push_async_callback(permit_cleanup.pop_all().aclose)
        resources.push_async_callback(self.backend.abort, self.owner_task)
        return True

    async def run(self) -> None:
        async with AsyncExitStack() as resources, AsyncExitStack() as permit_cleanup:
            if not await self.acquire(resources, permit_cleanup):
                return
            await self.run_native()

    def settle_unbound(self):
        """Read the result of the input owner's exact retirement transaction."""
        document = self.runner.inputs.dispositions.settle_unbound(self.original_keys)
        return document.shared_state(self.original_keys)

    async def fail(self, failure: Exception, cause: Exception, kind: type[TurnFailure]) -> NoReturn:
        """Settle and report once, retaining the exception that actually failed the turn."""
        await Coordination.run_worker(self.settle_unbound)
        outcome = await self.progress.report_failure(failure, kind)
        outcome.raise_fault(cause)

    async def run_native(self) -> None:
        try:
            await self.prepare_native()
            await self.stream()
        except asyncio.CancelledError:
            await self.runner.transition_turn(self.session_id, self.turn_lease, CancellingPhase())
            await self.backend.abort(self.owner_task)
            state = await Coordination.run_worker(self.settle_unbound)
            await self.progress.consumer.dispatch(events.PromptCancelled(state))
            raise
        except (NativeRequestFailure, Refused) as error:
            await self.fail(error, error, BackendFailed)
        except CoordinationError as error:
            await self.fail(error, error, CoordinationRefused)
        # The completed outcome owns notification delivery and request failure;
        # it is outside stream-fault reporting, so no failure is published twice.
        await self.progress.publish_result()
