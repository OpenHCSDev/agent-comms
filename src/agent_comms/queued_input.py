"""Live accepted input and the registry context that permits its handoff.

These values never recover prompts from disk. A renamed thread retains the same
incarnation birth and admission; the registry resolves its name before comparison.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from functools import partial
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any, Self

from acp import RequestError

from .child_process import ProcessIdentity
from .errors import RelationViolationError
from .goals import Goal
from .image_inputs import ImageInput
from .input_attempt import ACPInputIdText, InputAttempt, ReservedInput
from .input_origin import InputOrigin, UnattributedInputOrigin
from .store_files import _async_store_lock
from .coordinator import Coordination
from .turn_goal_permission import AcceptedGoalPermission
from .turn_input_source import AcceptedFollowingInput
from .thread_identity import AdmissionIdentity
from .threads import Thread

if TYPE_CHECKING:
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

    @classmethod
    async def require_capacity(cls, inputs: InputDrain, session_id: str) -> None:
        """Follow-ups cannot exceed the original live awaiting-start allowance."""
        document = await Coordination.run_worker(inputs.dispositions.read)
        pending = sum(
            not document.all_started(source.keys)
            for source in inputs.following_sources.get(session_id, {}).values()
        )
        if pending >= 32:
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
        async with _async_store_lock(inputs.comms._wire_lock_path), AsyncExitStack() as rollback:
            custody = ExitStack()
            rollback.push_async_callback(Coordination.run_worker, custody.close)
            await cls.require_capacity(inputs, session_id)
            item, owner, row = await Coordination.run_worker(partial(
                cls.capture, inputs, name, text=text, prompt=prompt, echo=echo,
                images=images, controller=controller, input_id=input_id,
                origin=origin, custody=custody,
            ))
            yield item, owner, row, custody

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
                def require_handoff():
                    snapshot = inputs.comms.registry.snapshot()
                    canonical = snapshot.canonical_name(name)
                    self.require_handoff(
                        snapshot, canonical, inputs.dispositions.read().lookup(self.key), self.input_id,
                    )
                    return canonical
                canonical = await Coordination.run_worker(require_handoff)
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


class InitialInput(QueuedInput):
    @classmethod
    async def require_capacity(cls, inputs: InputDrain, session_id: str) -> None:
        """A new original is not a following input in the live native queue."""

    @classmethod
    async def run(
        cls, inputs: InputDrain, session_id: str, thread_name: str, task: str, *,
        images: tuple[ImageInput, ...] = (), display_text: str | None = None,
        input_id: str | None = None,
        origin: InputOrigin = UnattributedInputOrigin(),
    ) -> None:
        """Own original reservation, dispatch and unbound retirement as one lifetime."""
        async with AsyncExitStack() as resources:
            async with cls.reserve(
                inputs, session_id, thread_name, text=display_text or task, prompt=task,
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

