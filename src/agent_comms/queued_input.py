"""Live accepted input and the registry context that permits its handoff.

These values never recover prompts from disk. A renamed thread retains the same
incarnation birth and admission; the registry resolves its name before comparison.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from functools import partial
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any, Self

from acp import RequestError

from .child_process import ProcessIdentity
from .errors import RelationViolationError
from .goals import Goal
from .image_inputs import ImageInput
from .assignment_states import DeferredAssignment, DeliveredAssignment
from .bus_publication import stable_thread_lookup
from .coordination_cohort import pending_sealed_assignments
from .coordination_errors import IdentityConflict, StaleRevision
from .input_attempt import ACPInputIdText, InputAttempt, ReservedInput
from .input_disposition import InputDispositions
from .input_origin import InputOrigin, UnattributedInputOrigin, WireInputOrigin
from .store_files import _async_store_lock
from .coordinator import Coordination
from .turn_goal_permission import AcceptedGoalPermission
from .turn_input_source import AcceptedFollowingInput, DeliveredFollowingInput
from .thread_identity import AdmissionIdentity
from .threads import Thread

if TYPE_CHECKING:
    from asyncio import Queue
    from .agent_backend import InputContent, InputRequest, SendNow, TurnInboxItem
    from .bus_publication import CommittedDelivery
    from .message_bus import MessageBus
    from .coordination_tables.assignments import WakeAssignment
    from .messages import Message
    from .acp_extension import QueueItem, QueueScope
    from .registry_document import RegistrySnapshot
    from .input_drain import InputDrain
    from .turn_runner import TurnRunner


class InputHandoffRefused(RelationViolationError):
    """Captured acceptance was refused before any native turn was dispatched."""


@dataclass(frozen=True, slots=True)
class QueuedInputContext:
    admission: AdmissionIdentity
    goal: Goal | None = None

    @classmethod
    def capture(cls, owner: Thread, admission: AdmissionIdentity):
        return cls(admission, owner.goal)

    def named(self, name: str) -> QueuedInputContext:
        """Use the caller's canonical registry name without changing authority."""
        return replace(
            self,
            admission=replace(
                self.admission,
                incarnation=replace(self.admission.incarnation, name=name),
            ),
        )

    def owns(self, admission: AdmissionIdentity) -> bool:
        return self.named(admission.incarnation.name).admission == admission

    @property
    def active_goal_id(self) -> str | None:
        return self.goal.id if self.goal is not None and self.goal.state.active else None


@dataclass(frozen=True, slots=True)
class QueuedInput:
    text: str
    echo: bool
    context: QueuedInputContext
    input_id: str
    prompt: str
    images: tuple[ImageInput, ...] = ()
    controller: Any = None

    @property
    def key(self) -> str:
        return f"acp:{self.input_id}"

    @property
    def accepted_id(self) -> str | None:
        return self.input_id

    def queue_items(self, scope: QueueScope) -> tuple[QueueItem, ...]:
        """Only this acceptance's original admission can appear in its queue."""
        from .acp_extension import QueueItem

        return (QueueItem(self.input_id, self.text),) if self.context.owns(scope.admission) else ()

    def source(self) -> AcceptedFollowingInput:
        return AcceptedFollowingInput(
            keys=(self.key,),
            accepted_id=self.input_id,
            goal_permission=AcceptedGoalPermission(self.context.active_goal_id),
        )

    @classmethod
    def capture(
        cls,
        inputs: InputDrain,
        name: str,
        *,
        text: str,
        prompt: str,
        echo: bool,
        images: tuple[ImageInput, ...],
        controller: Any,
        custody: ExitStack,
        input_id: str | None = None,
        origin: InputOrigin = UnattributedInputOrigin(),
    ) -> tuple[Self, Thread, InputAttempt]:
        """Called inside the wire boundary; acceptance follows the durable reservation."""
        snapshot = inputs.comms.registry.snapshot()
        canonical = snapshot.canonical_name(name)
        owner = snapshot.threads[canonical]
        context = QueuedInputContext.capture(
            owner,
            snapshot.admission_identity(canonical),
        )
        origin.require_ingress(inputs.comms, snapshot, context.admission, controller)
        item = cls(
            text or prompt or "[image prompt]",
            echo,
            context,
            ACPInputIdText.new() if input_id is None else ACPInputIdText.decode(input_id),
            prompt,
            images,
            controller,
        )
        row = ReservedInput(
            item.key, None, canonical, context.admission.admission_generation,
            canonical, item.text, origin=origin,
        )
        document = inputs.dispositions.reserve_originals(row, custody=custody)
        return item, owner, document.lookup(item.key)

    @staticmethod
    def remaining_capacity(inputs: InputDrain, session_id: str) -> int:
        """How many follow-ups the current locked transaction may still place."""
        sources = tuple(inputs.following_sources.get(session_id, {}).values())
        document = inputs.dispositions.read(tuple(key for source in sources for key in source.keys))
        return 32 - sum(not document.all_started(source.keys) for source in sources)

    @classmethod
    def require_capacity(cls, inputs: InputDrain, session_id: str) -> None:
        """Follow-ups cannot exceed the original live awaiting-start allowance."""
        if cls.remaining_capacity(inputs, session_id) <= 0:
            raise RequestError.invalid_params(
                {"reason": "Too many follow-up inputs awaiting their own user start."}
            )

    @classmethod
    @asynccontextmanager
    async def reserve(
        cls, inputs: InputDrain, session_id: str, name: str, *, text: str, prompt: str,
        echo: bool, images: tuple[ImageInput, ...], controller: Any,
        input_id: str | None = None,
        origin: InputOrigin = UnattributedInputOrigin(),
    ) -> AsyncIterator[tuple[Self, Thread, InputAttempt, ExitStack]]:
        """Hold original wire custody and join reservation rollback through handoff.

        The consumer transfers this original ExitStack only after binding its
        live input, or into its existing longer turn-acquisition lifetime.
        """
        async with AsyncExitStack() as rollback:
            custody = ExitStack()
            rollback.push_async_callback(Coordination.run_worker, custody.close)
            async with _async_store_lock(inputs.comms._wire_lock_path):
                cls.require_capacity(inputs, session_id)
                item, owner, row = cls.capture(
                    inputs, name, text=text, prompt=prompt, echo=echo,
                    images=images, controller=controller, input_id=input_id,
                    origin=origin, custody=custody,
                )
                yield item, owner, row, custody

    def place(
        self, inputs: InputDrain, session_id: str,
        inbox: Queue[TurnInboxItem], content: InputContent,
    ) -> None:
        """The accepted input owns transfer into this turn's existing native queue."""
        from .agent_backend import InputId, InputRequest, WhenBusy

        inputs.require_inbox(session_id, inbox)
        inputs.following_sources.setdefault(session_id, {})[self.input_id] = self.source()
        inputs.queued_inputs.setdefault(session_id, {})[self.input_id] = self
        inbox.put_nowait(InputRequest(
            input_id=InputId(self.input_id), content=content, when_busy=WhenBusy.STEER,
        ))

    def bind_turn(
        self, owner: Thread, admission: int, turn_id: str
    ) -> QueuedInput:
        return self

    def immediate(self) -> QueuedInput:
        return self

    def deferred(self, row: InputAttempt, owner: Thread) -> DeferredQueuedInput:
        return DeferredQueuedInput(
            **{field.name: getattr(self, field.name) for field in fields(QueuedInput)},
            receipt=row,
            turn_id=owner.active_turn.id if owner.active_turn else None,
        )

    async def dispatch(self, turns: TurnRunner, session_id: str, name: str) -> None:
        """Transfer this live acceptance under current authority; never recover disk work."""
        inputs = turns.inputs
        try:
            async with _async_store_lock(inputs.comms._wire_lock_path):
                self.require_live_source(inputs, session_id)
                snapshot = inputs.comms.registry.snapshot()
                canonical = snapshot.canonical_name(name)
                self.require_handoff(
                    snapshot, canonical, inputs.dispositions.lookup(self.key), self.input_id,
                )
        except RelationViolationError as error:
            raise InputHandoffRefused(str(error)) from error
        await turns.run_agent_turn(
            session_id,
            canonical,
            self.prompt,
            images=self.images,
            original_keys=(self.key,),
            initial_display_text=self.text if self.echo else None,
            original_owner_input=True,
            original_goal_id=self.context.active_goal_id,
            accepted_input_id=self.accepted_id,
        )

    def require_live_source(self, inputs: InputDrain, session_id: str) -> None:
        if inputs.following_sources.get(session_id, {}).get(self.input_id) != self.source():
            raise RelationViolationError("Accepted input source changed")

    def current(self, owner: Thread, admission: int) -> bool:
        return self.context.named(owner.name) == QueuedInputContext.capture(
            owner, AdmissionIdentity(owner.incarnation, admission)
        )

    def require_handoff(
        self,
        snapshot: RegistrySnapshot,
        name: str,
        row: InputAttempt,
        input_id: str,
    ) -> None:
        owner = snapshot.require_active(name)
        snapshot.statuses[owner.name].require_running()
        owner.require_local_process(ProcessIdentity.capture(os.getpid()))
        owner.require_idle()
        admission = snapshot.admission_generations[owner.name]
        if not self.current(owner, admission):
            raise RelationViolationError("Queued input acceptance context changed")
        if not row.queued_for(owner.incarnation, admission, self.text):
            raise RelationViolationError("Queued input reservation changed")
        if row.key != f"acp:{input_id}":
            raise RelationViolationError("Queued input ID changed")

    def after_clear(self) -> QueuedInput | None:
        return None

    def restore_after_turn(self) -> QueuedInput | None:
        return self.immediate() if self.echo else None

    async def started(self, inputs: InputDrain) -> str | None:
        """The running turn proved this input's start; the text the client shows as the user's."""
        return self.text

    async def not_started(self, inputs: InputDrain) -> None:
        """The turn refused this input or ended before its user start."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveredInput(QueuedInput):
    """A pending bus message handed to its recipient's running turn.

    Its assignment is paused while the turn holds it. A proved start settles it
    as delivered; an input never written returns it to pending for an ordinary
    wake. One written without a proved start stays paused and is never resent.
    """

    bus_key: str
    message: Message
    assignment: WakeAssignment

    @property
    def key(self) -> str:
        return self.bus_key

    def queue_items(self, scope: QueueScope) -> tuple[QueueItem, ...]:
        return ()  # The user's queue lists only what the user typed.

    def source(self) -> DeliveredFollowingInput:
        return DeliveredFollowingInput(
            keys=(self.key,),
            accepted_id=self.input_id,
            goal_permission=AcceptedGoalPermission(self.context.active_goal_id),
            origins=(self.message,),
        )

    @classmethod
    def reserve_pending(
        cls,
        inputs: InputDrain,
        name: str,
        root_id: str,
        pending: tuple[tuple[CommittedDelivery, WakeAssignment], ...],
        store: Coordination,
    ) -> tuple[tuple[Self, InputContent], ...]:
        """Called inside the wire boundary: reserve each input, then pause its assignment."""
        from .agent_backend import InputContent
        from .context_segments.wake import DeliveredMessageSegment
        from .routing import ScheduledTurn
        from .turn_context import TurnContext

        snapshot = inputs.comms.registry.snapshot()
        canonical = snapshot.canonical_name(name)
        owner = snapshot.threads[canonical]
        context = QueuedInputContext.capture(owner, snapshot.admission_identity(canonical))
        document = inputs.dispositions.read(
            tuple(InputDispositions.bus_key(source.message, owner) for source, _ in pending))
        reserved = []
        for source, assignment in pending:
            message = source.message
            key = InputDispositions.bus_key(message, owner)
            if document.lookup(key).exists:
                continue  # An earlier delivery already owns this message's input row.
            rendered = TurnContext.render_segments(
                (DeliveredMessageSegment.capture(source, assignment, owner),)
            )
            row = ReservedInput(
                key, message.seq, canonical, context.admission.admission_generation,
                message.target,
                ScheduledTurn.incoming(message, aliases=snapshot.aliases).prompt,
                origin=WireInputOrigin(root_id, message.reference),
            )
            with ExitStack() as custody:
                inputs.dispositions.reserve_originals(row, custody=custody)
                paused = store.assignments.transition_preengagement(
                    assignment.assignment_id, DeferredAssignment,
                    expected_revision=assignment.revision,
                ).value
                custody.pop_all()
            reserved.append((
                cls(
                    text=row.source_text, echo=False, context=context,
                    input_id=ACPInputIdText.new(), prompt=rendered.text,
                    bus_key=key, message=message, assignment=paused,
                ),
                InputContent(text=rendered.text, contributions=rendered.contributions),
            ))
        return tuple(reserved)

    @classmethod
    async def recover_before_drain(
        cls,
        inputs: InputDrain,
        session_id: str,
        root_id: str,
        drain: Callable[[str, str], Awaitable[int]],
    ) -> int:
        """Restore positively unsent delivery before the ordinary selector reads work."""
        await cls.recover_interrupted(inputs, session_id, root_id)
        return await drain(session_id, root_id)

    @classmethod
    async def recover_interrupted(
        cls, inputs: InputDrain, session_id: str, root_id: str,
    ) -> int:
        """Requeue only deliveries positively proved absent from native input.

        A killed owner can lose the live ``DeliveredInput`` which normally
        restores its paused assignment. The durable input row and native source
        membership replace that lost object: any send binding or native row
        keeps the assignment deferred and therefore never replayed.
        """
        async with _async_store_lock(inputs.comms._wire_lock_path):
            snapshot = inputs.comms.registry.snapshot()
            owner = snapshot.require(inputs.sessions.require(session_id))
            lookup = stable_thread_lookup(owner.created_at)
            document = inputs.dispositions.unresolved(
                inputs.comms.registry.aliases_for(owner.name))
            live_keys = {
                item.key for item in inputs.queued_inputs.get(session_id, {}).values()
            }
            rows = {
                reference: row
                for reference, row in document.unbound_wire_inputs(root_id).items()
                if row.key not in live_keys
            }
            with Coordination(str(inputs.comms.root / "coordination.sqlite3")) as store:
                assignments = store.assignments.recoverable_unbound_deliveries(
                    lookup, frozenset(rows)
                )
                recoverable = tuple(
                    (rows[assignment.source], assignment) for assignment in assignments
                )
                if not recoverable:
                    return 0
                inputs.dispositions.settle_unbound(tuple(row.key for row, _ in recoverable))
                restored = 0
                for _row, assignment in recoverable:
                    try:
                        store.assignments.restore_unbound_delivery(assignment)
                    except StaleRevision:
                        continue
                    restored += 1
                return restored

    @classmethod
    async def deliver_pending(
        cls, inputs: InputDrain, session_id: str, owner: Thread, bus: MessageBus, root_id: str, after_seq: int,
    ) -> None:
        """Hand this owner's pending bus messages to its running turn.

        Pi takes each one after its current tool batch. A turn that cannot take
        follow-ups leaves them pending for the ordinary wake after it ends.
        """
        inbox = await inputs.effects.turns.active_backend_inbox(session_id)
        # A turn takes later inputs once its first one is in.
        if inbox is None or session_id not in inputs.original_sources:
            return
        store_path = inputs.comms.root / "coordination.sqlite3"
        lookup = stable_thread_lookup(owner.created_at)

        def read(store: Coordination):
            assignments = pending_sealed_assignments(
                store, lookup, owner.name, after_seq=after_seq
            )
            with bus.log.certified_read() as source:
                if source.marker.root_id != root_id:
                    raise IdentityConflict("delivered source wire root changed")
                originals = tuple(source.references(tuple(
                    assignment.source for assignment in assignments
                )))
            return tuple(zip(originals, assignments, strict=True))

        pending = await Coordination.run_async(store_path, read)
        if not pending:
            return
        unplaced: tuple[Self, ...] = ()
        async with _async_store_lock(inputs.comms._wire_lock_path):
            # Placement owns asyncio.Queue and the live input maps, so it must
            # remain on their event-loop thread. The lock acquisition alone is
            # asynchronous; this complete transaction contains no await.
            with Coordination(str(store_path)) as store:
                accepted = pending[:max(cls.remaining_capacity(inputs, session_id), 0)]
                reserved = cls.reserve_pending(inputs, owner.name, root_id, accepted, store)
            for index, (item, content) in enumerate(reserved):
                try:
                    item.place(inputs, session_id, inbox, content)
                except InputHandoffRefused:
                    unplaced = tuple(item for item, _ in reserved[index:])
                    break
        for item in unplaced:
            await item.not_started(inputs)

    async def dispatch(self, turns: TurnRunner, session_id: str, name: str) -> None:
        raise InputHandoffRefused("A bus message its turn never took wakes its owner instead")

    def after_clear(self) -> DeliveredInput:
        return self  # Clearing the user's queue cannot withdraw a bus message.

    def restore_after_turn(self) -> None:
        return None

    async def settle(self, inputs: InputDrain, state) -> None:
        await Coordination.run_async(
            inputs.comms.root / "coordination.sqlite3",
            lambda store: store.assignments.transition_preengagement(
                self.assignment.assignment_id, state,
                expected_revision=self.assignment.revision,
            ),
        )

    async def started(self, inputs: InputDrain) -> None:
        # Its transcript row is the bus message, not typed user text.
        await self.settle(inputs, DeliveredAssignment)

    async def not_started(self, inputs: InputDrain) -> None:
        document = await Coordination.run_worker(
            partial(inputs.finish_original_inputs, (self.key,))
        )
        if document.lookup(self.key).has_native_binding:
            return  # Written without a proved start: it stays paused, never resent.
        await self.settle(inputs, self.assignment.lifecycle.mode.initial_state())


class InitialInput(QueuedInput):
    @classmethod
    def require_capacity(cls, inputs: InputDrain, session_id: str) -> None:
        """A new original is not a following input in the live native queue."""

    @classmethod
    async def run(
        cls, inputs: InputDrain, session_id: str, thread_name: str, task: str, *,
        images: tuple[ImageInput, ...] = (), display_text: str | None = None,
        input_id: str | None = None,
        origin: InputOrigin = UnattributedInputOrigin(),
    ) -> None:
        """Own original reservation, dispatch and unbound retirement as one lifetime."""
        from .turn_context import TurnContext, UserRequestSegment

        request = UserRequestSegment.capture(task)
        async with AsyncExitStack() as resources:
            async with cls.reserve(
                inputs, session_id, thread_name, text=display_text or task,
                prompt=TurnContext.render_segments((request,), images=images).text,
                echo=display_text is not None, images=images,
                controller=inputs.runtime.controller.get(), input_id=input_id, origin=origin,
            ) as (item, _owner, row, custody):
                inputs.queued_inputs.setdefault(session_id, {})[item.input_id] = item
                resources.push_async_callback(item.finish, inputs, session_id)
                custody.pop_all()
            await inputs.emit_input_disposition(session_id, row)
            await inputs.emit_queue_state(session_id)
            await item.dispatch(inputs.effects.turns, session_id, thread_name)

    async def finish(self, inputs: InputDrain, session_id: str) -> None:
        """Retire only this captured original after its dispatch resources join."""
        queued = inputs.queued_inputs.get(session_id, {})
        if queued.get(self.input_id) is self:
            try:
                await Coordination.run_worker(partial(
                    inputs.finish_original_inputs, (self.key,),
                ))
            finally:
                # The joined original write finishes before cancellation is
                # propagated. Burn only this live grant; durable UNKNOWN stays.
                queued.pop(self.input_id)
            await inputs.emit_queue_state(session_id)

    def require_live_source(self, inputs: InputDrain, session_id: str) -> None:
        if inputs.queued_inputs.get(session_id, {}).get(self.input_id) is not self:
            raise RelationViolationError("Original input acceptance changed")

    def after_clear(self) -> InitialInput:
        return self  # Clearing follow-ups cannot withdraw the original dispatch.

    def restore_after_turn(self) -> None:
        return None  # Its original terminal disposition owns cancellation/UNKNOWN.

    @property
    def accepted_id(self) -> None:
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeferredQueuedInput(QueuedInput):
    receipt: InputAttempt
    turn_id: str | None

    def bind_turn(
        self, owner: Thread, admission: int, turn_id: str
    ) -> QueuedInput:
        return replace(self, turn_id=turn_id) if self.current(owner, admission) else self

    def immediate(self) -> QueuedInput:
        return QueuedInput(
            **{field.name: getattr(self, field.name) for field in fields(QueuedInput)}
        )

