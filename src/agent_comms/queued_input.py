"""Live accepted input and the registry context that permits its handoff.

These values never recover prompts from disk. A renamed thread retains the same
incarnation birth and admission; the registry resolves its name before comparison.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any

from .child_process import ProcessIdentity
from .errors import RelationViolationError
from .goal_waits import GoalWait
from .goals import Goal
from .image_inputs import ImageInput
from .input_attempt import ACPInputIdText, InputAttempt
from .store_files import _store_lock
from .turn_goal_permission import AcceptedGoalPermission
from .turn_input_source import AcceptedFollowingInput
from .thread_identity import AdmissionIdentity
from .threads import Thread

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot
    from .input_drain import InputDrain
    from .turn_runner import TurnRunner


class InputHandoffRefused(RelationViolationError):
    """Captured acceptance was refused before any native turn was dispatched."""


@dataclass(frozen=True, slots=True)
class QueuedInputContext:
    admission: AdmissionIdentity
    goal: Goal | None = None
    wait: GoalWait | None = None

    @classmethod
    def capture(cls, owner: Thread, admission: AdmissionIdentity, wait: GoalWait | None):
        return cls(admission, owner.goal, wait)

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
        input_id: str | None = None,
    ) -> tuple[QueuedInput, Thread]:
        """Called inside the wire boundary; acceptance follows the durable reservation."""
        snapshot = inputs.comms.registry.snapshot()
        canonical = snapshot.aliases.get(name, name)
        owner = snapshot.threads[canonical]
        context = QueuedInputContext.capture(
            owner,
            snapshot.admission_identity(canonical),
            inputs.comms.goals.goal_wait(canonical),
        )
        item = cls(
            text or prompt or "[image prompt]",
            echo,
            context,
            ACPInputIdText.new() if input_id is None else ACPInputIdText.decode(input_id),
            prompt,
            images,
            controller,
        )
        if not inputs.dispositions.record(
            item.key,
            seq=None,
            owner=canonical,
            admission=context.admission.admission_generation,
            target=canonical,
            text=item.text,
        ):
            raise RelationViolationError("Input reservation already exists")
        return item, owner

    def bind_turn(
        self, owner: Thread, admission: int, wait: GoalWait | None, turn_id: str
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
            with _store_lock(inputs.comms._wire_lock_path):
                self.require_live_source(inputs, session_id)
                snapshot = inputs.comms.registry.snapshot()
                canonical = snapshot.aliases.get(name, name)
                self.require_handoff(
                    snapshot,
                    canonical,
                    inputs.comms.goals.goal_wait(canonical),
                    inputs.dispositions.read().lookup(self.key),
                    self.input_id,
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

    def current(self, owner: Thread, admission: int, wait: GoalWait | None) -> bool:
        return self.context.named(owner.name) == QueuedInputContext.capture(
            owner, AdmissionIdentity(owner.incarnation, admission), wait
        )

    def require_handoff(
        self,
        snapshot: RegistrySnapshot,
        name: str,
        wait: GoalWait | None,
        row: InputAttempt,
        input_id: str,
    ) -> None:
        owner = snapshot.require_active(name)
        snapshot.statuses[owner.name].require_running()
        owner.require_local_process(ProcessIdentity.capture(os.getpid()))
        owner.require_idle()
        admission = snapshot.admission_generations[owner.name]
        if not self.current(owner, admission, wait):
            raise RelationViolationError("Queued input acceptance context changed")
        if not row.queued_for(owner.incarnation, admission, self.text):
            raise RelationViolationError("Queued input reservation changed")
        if row.key != f"acp:{input_id}":
            raise RelationViolationError("Queued input ID changed")

    def future_receipt(self, owner: Thread) -> InputAttempt | None:
        return None

    def after_clear(self) -> QueuedInput | None:
        return None

    def restore_after_turn(self) -> QueuedInput | None:
        return self.immediate() if self.echo else None


class InitialInput(QueuedInput):
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
        self, owner: Thread, admission: int, wait: GoalWait | None, turn_id: str
    ) -> QueuedInput:
        return replace(self, turn_id=turn_id) if self.current(owner, admission, wait) else self

    def immediate(self) -> QueuedInput:
        return QueuedInput(
            **{field.name: getattr(self, field.name) for field in fields(QueuedInput)}
        )

    def future_receipt(self, owner: Thread) -> InputAttempt | None:
        turn = owner.active_turn
        if turn is None or self.turn_id != turn.id:
            return None
        if self.context.owns(AdmissionIdentity(owner.incarnation, turn.admission_generation)):
            return self.receipt
        return None
