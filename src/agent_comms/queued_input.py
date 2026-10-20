"""Live accepted input and the registry context that permits its handoff.

These values never recover prompts from disk. A renamed thread retains the same
birth identity and input generation; the registry resolves its name before comparison.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from dataclasses import field, dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Generic, Self, TypeVar

from acp import RequestError

from .assignment_states import DeferredAssignment, DeliveredAssignment
from .child_process import ProcessIdentity
from .coordination_cohort import pending_sealed_assignments
from .coordination_errors import IdentityConflict, StaleRevision
from .coordination_session import ReadNativeCoordinationSession
from .coordinator import Coordination
from .errors import RelationViolationError
from .field_codec import Id128Text
from .goals import GoalDeclaration, NoGoal
from .image_inputs import ImageInput
from .input_attempt import InputAttempt, ReservedInput
from .input_disposition import InputDispositions
from .input_origin import InputOrigin, UnattributedInputOrigin, WireInputOrigin
from .store_files import _store_lock
from .thread_identity import ClaimedIdentity
from .threads import Thread
from .turn_goal_permission import AcceptedGoalPermission
from .turn_input_source import AcceptedFollowingInput, DeliveredFollowingInput

if TYPE_CHECKING:
    from .acp_extension import QueueItem, QueueScope
    from .agent_backend import InputContent, TurnInbox
    from .bus_publication import CommittedDelivery
    from .comms import Comms
    from .coordination_tables.assignments import WakeAssignment
    from .input_drain import InputDrain
    from .message_bus import MessageBus
    from .messages import Message
    from .registry_document import RegistrySnapshot
    from .turn_input_source import TurnInputSource
    from .turn_runner import TurnRunner


class InputHandoffRefused(RelationViolationError):
    """Captured acceptance was refused before any native turn was dispatched."""


@dataclass(frozen=True, slots=True)
class QueuedInputContext:
    claim_scope: ClaimedIdentity = field(metadata={"wire_name": 'admission'})
    goal: GoalDeclaration = field(default_factory=NoGoal)

    @classmethod
    def capture(cls, owner: Thread, claim_scope: ClaimedIdentity):
        return cls(claim_scope, owner.goal)

    def named(self, name: str) -> QueuedInputContext:
        """Use the caller's canonical registry name without changing authority."""
        return replace(
            self,
            claim_scope=replace(
                self.claim_scope,
                incarnation=replace(self.claim_scope.incarnation, name=name),
            ),
        )

    def owns(self, claim_scope: ClaimedIdentity) -> bool:
        return self.named(claim_scope.incarnation.name).claim_scope == claim_scope

    @property
    def active_goal_id(self) -> str | None:
        return self.goal.active().reported_id()


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

    def queue_items(self, scope: QueueScope) -> tuple[QueueItem, ...]:
        """Show the item only when its recorded input generation matches the queue."""
        from .acp_extension import QueueItem

        return (QueueItem(self.input_id, self.text),) if self.context.owns(scope.claim_scope) else ()

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
        ownership: ExitStack,
        input_id: str | None = None,
        origin: InputOrigin = UnattributedInputOrigin(),
    ) -> tuple[Self, InputAttempt]:
        """Called inside the wire boundary; acceptance follows the durable reservation."""
        snapshot = inputs.comms.registry.snapshot()
        canonical = snapshot.canonical_name(name)
        owner = snapshot.threads[canonical]
        context = QueuedInputContext.capture(
            owner,
            snapshot.claimed_identity(canonical),
        )
        origin.require_ingress(inputs.comms, snapshot, context.claim_scope, controller)
        item = cls(
            text or prompt or "[image prompt]",
            echo,
            context,
            Id128Text.mint() if input_id is None else Id128Text.decode(input_id),
            prompt,
            images,
            controller,
        )
        row = ReservedInput(
            item.key, None, canonical, context.claim_scope.claimed_generation,
            canonical, item.text, origin=origin,
        )
        document = inputs.dispositions.reserve_originals(row, ownership=ownership)
        return item, document.lookup(item.key)

    @staticmethod
    def remaining_capacity(inputs: InputDrain, sources: tuple[TurnInputSource, ...]) -> int:
        """Read capacity for the loop owner's captured following sources."""
        document = inputs.dispositions.read(tuple(key for source in sources for key in source.keys))
        return 32 - sum(not document.all_started(source.keys) for source in sources)

    @classmethod
    def require_capacity(cls, inputs: InputDrain, sources: tuple[TurnInputSource, ...]) -> None:
        """Follow-ups cannot exceed the original live awaiting-start allowance."""
        if cls.remaining_capacity(inputs, sources) <= 0:
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
    ) -> AsyncIterator[InputReservation[Self]]:
        """Serialize local queue placement; reserve durable input in a worker."""
        async with AsyncExitStack() as rollback:
            ownership = ExitStack()
            rollback.push_async_callback(Coordination.run_worker, ownership.close)
            async with inputs.queue_lock:
                sources = tuple(inputs.state(session_id).following.values())

                def capture():
                    with _store_lock(inputs.comms._wire_lock_path):
                        cls.require_capacity(inputs, sources)
                        return cls.capture(
                            inputs, name, text=text, prompt=prompt, echo=echo,
                            images=images, controller=controller, input_id=input_id,
                            origin=origin, ownership=ownership,
                        )

                item, row = await Coordination.run_worker(capture)
                yield InputReservation(item, row, rollback)

    def place(
        self, inputs: InputDrain, session_id: str,
        inbox: TurnInbox, content: InputContent,
    ) -> None:
        """The accepted input owns transfer into this turn's existing native queue."""
        from .agent_backend import InputId, InputRequest

        inputs.require_inbox(session_id, inbox)
        source = self.source()
        inputs.state(session_id).following[self.input_id] = source
        inputs.state(session_id).queued[self.input_id] = self
        inbox.put_nowait(InputRequest(
            input_id=InputId(self.input_id), content=content, when_busy=source.when_busy,
        ))

    async def dispatch(self, turns: TurnRunner, session_id: str, name: str) -> None:
        """Transfer this live acceptance under current authority; never recover disk work."""
        inputs = turns.inputs
        try:
            async with inputs.queue_lock:
                self.require_live_source(inputs, session_id)

                def verify():
                    with _store_lock(inputs.comms._wire_lock_path):
                        snapshot = inputs.comms.registry.snapshot()
                        canonical = snapshot.canonical_name(name)
                        self.require_handoff(
                            snapshot, canonical, inputs.dispositions.lookup(self.key), self.input_id,
                        )
                        return canonical

                canonical = await Coordination.run_worker(verify)
                self.require_live_source(inputs, session_id)
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
            accepted_input_id=self.input_id,
        )

    def is_live(self, inputs: InputDrain, session_id: str) -> bool:
        return inputs.state(session_id).following.get(self.input_id) == self.source()

    def require_live_source(self, inputs: InputDrain, session_id: str) -> None:
        if not self.is_live(inputs, session_id):
            raise RelationViolationError("Accepted input source changed")

    def current(self, owner: Thread, claim_scope: int) -> bool:
        return self.context.named(owner.name) == QueuedInputContext.capture(
            owner, ClaimedIdentity(owner.incarnation, claim_scope)
        )

    def require_handoff(
        self,
        snapshot: RegistrySnapshot,
        name: str,
        row: InputAttempt,
        input_id: str,
    ) -> None:
        owner = snapshot.require_active(name)
        snapshot.status(owner.name).require_running()
        owner.require_local_process(ProcessIdentity.capture(os.getpid()))
        owner.turn_record.read().require_idle()
        claim_scope = snapshot.claimed_generations[owner.name]
        if not self.current(owner, claim_scope):
            raise RelationViolationError("Queued input acceptance context changed")
        if not row.queued_for(owner.incarnation, claim_scope, self.text):
            raise RelationViolationError("Queued input reservation changed")
        if row.key != f"acp:{input_id}":
            raise RelationViolationError("Queued input ID changed")

    def after_clear(self) -> QueuedInput | None:
        return None

    def restore_after_turn(self) -> QueuedInput | None:
        return self if self.echo else None

    async def started(self, inputs: InputDrain) -> str | None:
        """The running turn proved this input's start; the text the client shows as the user's."""
        return self.text

    async def not_started(self, inputs: InputDrain) -> None:
        """The turn refused this input or ended before its user start."""


ReservedQueuedInput = TypeVar("ReservedQueuedInput", bound=QueuedInput)


@dataclass(frozen=True, slots=True)
class InputReservation(Generic[ReservedQueuedInput]):
    """A captured input and its rollback until placement takes ownership."""

    item: ReservedQueuedInput
    source: InputAttempt
    _rollback: AsyncExitStack

    def release(self) -> AsyncExitStack:
        """Transfer rollback actions; a new scope may adopt them or discard them."""
        return self._rollback.pop_all()


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
        cls, inputs: InputDrain, name: str, root_id: str,
        pending: tuple[tuple[CommittedDelivery, WakeAssignment], ...],
        sources: tuple[TurnInputSource, ...],
        reserved: list[tuple[Self, InputContent]],
    ) -> None:
        """Fill the original reservation scope in a joined, wire-locked worker.

        The caller owns ``reserved`` and its AsyncExitStack before work starts,
        so cancellation cannot lose a successfully paused but unplaced input.
        The worker opens/closes SQLite and never reads or mutates live queues.
        """
        from .agent_backend import InputContent
        from .context_segments.wake import DeliveredMessageSegment
        from .routing import ScheduledTurn
        from .turn_context import TurnContext

        with _store_lock(inputs.comms._wire_lock_path):
            accepted = pending[:max(cls.remaining_capacity(inputs, sources), 0)]
            if not accepted:
                return
            snapshot = inputs.comms.registry.snapshot()
            canonical = snapshot.canonical_name(name)
            owner = snapshot.threads[canonical]
            context = QueuedInputContext.capture(owner, snapshot.claimed_identity(canonical))
            document = inputs.dispositions.read(tuple(
                InputDispositions.bus_key(source.message, owner) for source, _ in accepted
            ))
            with Coordination(str(inputs.comms.root / "coordination.sqlite3")) as store:
                for source, assignment in accepted:
                    message = source.message
                    key = InputDispositions.bus_key(message, owner)
                    if document.lookup(key).exists:
                        continue
                    rendered = TurnContext.render_segments(
                        (DeliveredMessageSegment.capture(source, assignment, owner),)
                    )
                    row = ReservedInput(
                        key, message.seq, canonical, context.claim_scope.claimed_generation,
                        message.target,
                        ScheduledTurn.incoming(message, aliases=snapshot.aliases).prompt,
                        origin=WireInputOrigin(root_id, message.reference),
                    )
                    with ExitStack() as ownership:
                        inputs.dispositions.reserve_originals(row, ownership=ownership)
                        paused = store.assignments.transition_preengagement(
                            assignment.assignment_id, DeferredAssignment,
                            expected_revision=assignment.revision,
                        ).value
                        ownership.callback(store.assignments.restore_unbound_delivery, paused)
                        reserved.append((
                            cls(
                                text=row.source_text, echo=False, context=context,
                                input_id=Id128Text.mint(), prompt=rendered.text,
                                bus_key=key, message=message, assignment=paused,
                            ),
                            InputContent(text=rendered.text, contributions=rendered.contributions),
                        ))
                        ownership.pop_all()

    @staticmethod
    async def _restore_unplaced(
        inputs: InputDrain, session_id: str,
        reserved: list[tuple[DeliveredInput, InputContent]],
    ) -> None:
        # Capture the loop-owned transfer before the first await; a turn may
        # retire its placed inputs while an unplaced input is being restored.
        placed = inputs.state(session_id).queued
        unplaced = tuple(
            item for item, _content in reserved if placed.get(item.input_id) is not item
        )
        for item in unplaced:
            await item.not_started(inputs)

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
        async with inputs.queue_lock:
            name = inputs.sessions.require(session_id)
            live_keys = frozenset(
                item.key for item in inputs.state(session_id).queued.values()
            )
            return await Coordination.run_worker(partial(
                cls._recover_locked, inputs.comms, inputs.dispositions,
                name, root_id, live_keys,
            ))

    @staticmethod
    def _recover_locked(
        comms: Comms, dispositions: InputDispositions,
        name: str, root_id: str, live_keys: frozenset[str],
    ) -> int:
        """Acquire, recover and release entirely inside the joined worker."""
        entry = comms.registry.entry(name).entry
        owner = entry.thread
        document = dispositions.unresolved(entry.names)
        if not any(row.key not in live_keys
                   for row in document.unbound_wire_inputs(root_id).values()):
            return 0
        with _store_lock(comms._wire_lock_path):
            entry = comms.registry.entry(name).entry
            owner = entry.thread
            document = dispositions.unresolved(entry.names)
            rows = {
                reference: row
                for reference, row in document.unbound_wire_inputs(root_id).items()
                if row.key not in live_keys
            }
            if not rows:
                return 0  # Without work, no SQLite connection is needed, let alone a writer.
            path = str(comms.root / "coordination.sqlite3")
            with Coordination(path, session_type=ReadNativeCoordinationSession) as store:
                assignments = store.assignments.recoverable_unbound_deliveries(
                    owner.incarnation.lookup, frozenset(rows),
                )
            if not assignments:
                return 0
            with Coordination(path) as store:
                dispositions.settle_unbound(tuple(rows[row.source].key for row in assignments))
                restored = 0
                for assignment in assignments:
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
        if inbox is None or not inputs.state(session_id).turn_started():
            return
        store_path = inputs.comms.root / "coordination.sqlite3"
        lookup = owner.incarnation.lookup

        def read(store: Coordination):
            with store.session.read():
                assignments = tuple(
                    assignment for assignment in pending_sealed_assignments(
                        store, lookup, owner.name, after_seq=after_seq
                    ) if assignment.audience.joins_active_turn()
                )
            with bus.log.checked_read() as source:
                if source.marker.root_id != root_id:
                    raise IdentityConflict("delivered source wire root changed")
                originals = tuple(source.references(tuple(
                    assignment.source for assignment in assignments
                )))
            return tuple(zip(originals, assignments, strict=True))

        pending = await Coordination.run_read_async(store_path, read)
        if not pending:
            return
        reserved: list[tuple[Self, InputContent]] = []
        async with AsyncExitStack() as rollback:
            rollback.push_async_callback(cls._restore_unplaced, inputs, session_id, reserved)
            async with inputs.queue_lock:
                sources = tuple(inputs.state(session_id).following.values())
                await Coordination.run_worker(partial(
                    cls.reserve_pending, inputs, owner.name, root_id, pending, sources, reserved,
                ))
                for item, content in reserved:
                    try:
                        item.place(inputs, session_id, inbox, content)
                    except InputHandoffRefused:
                        break

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
    def require_capacity(cls, inputs: InputDrain, sources: tuple[TurnInputSource, ...]) -> None:
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
            ) as reservation:
                item = reservation.item
                inputs.state(session_id).queued[item.input_id] = item
                resources.push_async_callback(item.finish, inputs, session_id)
                reservation.release()
            await inputs.emit_input_disposition(session_id, reservation.source)
            await inputs.emit_queue_state(session_id)
            await item.dispatch(inputs.effects.turns, session_id, thread_name)

    async def finish(self, inputs: InputDrain, session_id: str) -> None:
        """Retire only this captured original after its dispatch resources join."""
        queued = inputs.state(session_id).queued
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

    def is_live(self, inputs: InputDrain, session_id: str) -> bool:
        return inputs.state(session_id).queued.get(self.input_id) is self

    def after_clear(self) -> InitialInput:
        return self  # Clearing follow-ups cannot withdraw the original dispatch.

    def restore_after_turn(self) -> None:
        return None  # Its original terminal disposition owns cancellation/UNKNOWN.
