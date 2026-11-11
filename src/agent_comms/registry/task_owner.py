"""The actual thread reads required by task emission and applicability."""

from abc import abstractmethod
from collections.abc import Callable

from agent_comms.execution.context_owner import ContextOwner
from agent_comms.nominal.field_codec import TurnId
from agent_comms.registry.thread_identity import TurnFence, TurnIdentity
from agent_comms.registry.thread_role_source import ThreadRoleSource


class TaskOwner(ContextOwner, ThreadRoleSource):
    __slots__ = ()

    @abstractmethod
    def require_publication_turn(self, claim_generation: Callable[[], int], turn_id: TurnId) -> None: ...

    @abstractmethod
    def require_turn_lease(self) -> TurnFence: ...

    @abstractmethod
    def has_authored_turn(self, identity: TurnIdentity, turn: TurnId) -> bool: ...

    @abstractmethod
    def has_observed_task_turn(self, identity: TurnIdentity, turn: TurnId) -> bool: ...
