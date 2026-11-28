from abc import abstractmethod

from agent_comms.planning.goal_work import GoalOwner
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.presence_reads import ProcessOwner
from agent_comms.registry.thread_identity import ObservedTurn


class GoalFailureOwner(ProcessOwner, GoalOwner):
    worktree: Checkout

    @abstractmethod
    def observed_turn(self, claim_scope: int) -> ObservedTurn: ...
