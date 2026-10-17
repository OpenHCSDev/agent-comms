"""A selected operator plan remains bound to its original live ACP controller."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .coordination_errors import IdentityConflict
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
        self, assignment: WakeAssignment, owner: Thread, operation_id: str
    ) -> None: ...

    @abstractmethod
    def applied(self, assignment: WakeAssignment, owner: Thread, operation_id: str) -> None: ...


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

    def require_current(self, assignment, owner, operation_id):
        from .runtime import SocketClient

        bound = self.agent._selected_write_controllers.get((owner.name, assignment.wire_seq))
        if bound is None or bound[0] != operation_id:
            raise IdentityConflict("Selected write original controller is no longer bound")
        controller = bound[1]
        if isinstance(controller, SocketClient):
            if not self.agent._runtime.is_controller(self.session_id, controller):
                raise IdentityConflict("Selected write controller disconnected")
        elif controller is not self.agent.sessions.client or controller is None:
            raise IdentityConflict("Selected write ACP controller changed")

    def applied(self, assignment, owner, operation_id):
        self.plans.applied(assignment, owner, operation_id)
        self.agent._selected_write_controllers.pop((owner.name, assignment.wire_seq), None)
