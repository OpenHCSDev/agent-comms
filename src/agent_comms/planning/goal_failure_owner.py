from abc import abstractmethod

from agent_comms.execution.child_process import ProcessState
from agent_comms.planning.goal_work import GoalOwner
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import ObservedTurn


class GoalFailureOwner(GoalOwner):
    __slots__ = ()
    worktree: Checkout
    process_identity: ProcessState

    @abstractmethod
    def observed_turn(self, claim_scope: int) -> ObservedTurn: ...
