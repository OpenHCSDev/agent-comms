"""A selected operator plan remains bound to its original live ACP controller."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .coordination_errors import IdentityConflict
from .field_codec import ClaimId
from .selected_write_plan import SelectedWritePlans

if TYPE_CHECKING:
    from .acp import CommsAgent
    from .coordination_tables.assignments import WakeAssignment
    from .selected_actions import SelectedAction
    from .threads import Thread


class SelectedWriteAuthority(ABC):
    @abstractmethod
    def select(
        self, assignment: WakeAssignment, owner: Thread, generation: int, action: SelectedAction
    ) -> SelectedAction: ...


class NoSelectedWritePlans(SelectedWriteAuthority):
    def select(self, assignment, owner, generation, action):
        return action


class BoundSelectedWriteAuthority(SelectedWriteAuthority):
    @abstractmethod
    def require_current(
        self, assignment: WakeAssignment, owner: Thread, operation_id: ClaimId
    ) -> None: ...

    @abstractmethod
    def applied(self, assignment: WakeAssignment, owner: Thread, operation_id: ClaimId) -> None: ...


@dataclass
class AcpSelectedWriteAuthority(BoundSelectedWriteAuthority):
    agent: CommsAgent
    session_id: str
    plans: SelectedWritePlans

    def select(self, assignment, owner, generation, action):
        plan = self.plans.load(assignment, owner, generation)
        if plan is None:
            return action
        self.require_current(assignment, owner, plan.operation_id)
        return action.with_plan(self, plan, assignment, owner)

    def require_current(
        self, assignment: WakeAssignment, owner: Thread, operation_id: ClaimId
    ) -> None:
        bound = self.agent._selected_write_controllers.get((owner.name, assignment.wire_seq))
        if bound is None or bound[0] != operation_id:
            raise IdentityConflict("Selected write original controller is no longer bound")
        controller = bound[1]
        controller.require_current(self, self.session_id)

    def applied(self, assignment: WakeAssignment, owner: Thread, operation_id: ClaimId) -> None:
        self.plans.applied(assignment, owner, operation_id)
        self.agent._selected_write_controllers.pop((owner.name, assignment.wire_seq), None)
