"""Live accepted input and the registry context that permits its handoff.

These values never recover prompts from disk. A renamed thread retains the same
incarnation birth and admission; the registry resolves its name before comparison.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .child_process import ProcessIdentity
from .errors import RelationViolationError
from .goal_waits import GoalWait
from .goals import Goal
from .image_inputs import ImageInput
from .input_attempt import InputAttempt
from .thread_identity import OwnerIdentity
from .threads import Thread

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot


@dataclass(frozen=True, slots=True)
class QueuedInputContext:
    owner: OwnerIdentity
    goal: Goal | None = None
    wait: GoalWait | None = None

    @classmethod
    def capture(cls, owner: Thread, admission: int, wait: GoalWait | None):
        return cls(owner.owner_identity(admission), owner.goal, wait)

    def named(self, name: str) -> QueuedInputContext:
        """Use the caller's canonical registry name without changing authority."""
        return replace(self, owner=replace(
            self.owner, incarnation=replace(self.owner.incarnation, name=name),
        ))

    def owns(self, owner: OwnerIdentity) -> bool:
        return self.named(owner.incarnation.name).owner == owner

    @property
    def active_goal_id(self) -> str | None:
        return self.goal.id if self.goal is not None and self.goal.state.active else None


@dataclass(frozen=True, slots=True)
class QueuedInput:
    text: str
    echo: bool
    context: QueuedInputContext
    receipt: InputAttempt | None = None
    turn_id: str | None = None
    images: tuple[ImageInput, ...] = ()
    controller: Any = None

    def current(self, owner: Thread, admission: int, wait: GoalWait | None) -> bool:
        return self.context.named(owner.name) == QueuedInputContext.capture(owner, admission, wait)

    def require_handoff(
        self, snapshot: RegistrySnapshot, name: str, wait: GoalWait | None,
        row: InputAttempt, input_id: str,
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
        turn = owner.active_turn
        if turn is None:
            return None
        if self.turn_id != turn.id:
            return None
        if self.context.owns(owner.owner_identity(turn.admission_generation)):
            return self.receipt
        return None
