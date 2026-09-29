"""Restart preflight refusals own whether an unsignalled request may wait."""

from abc import abstractmethod
from typing import ClassVar

from .declared_family import DeclaredFamily
from .errors import RelationViolationError


class RestartRefusal(RelationViolationError, DeclaredFamily, affix="Refusal"):
    message: ClassVar[str]

    def __init__(self):
        super().__init__(self.message)

    @abstractmethod
    def queue_state(self): ...


class WaitForIdle:
    def queue_state(self):
        from .restart_queue import PendingRestart

        return PendingRestart()


class ChangedSelection:
    def queue_state(self):
        from .restart_queue import StaleRestart

        return StaleRestart(reason=str(self))


class OwnerBusyRefusal(WaitForIdle, RestartRefusal):
    message = "Wait until the owner is idle before restart."


class OwnerChangedBeforeFenceRefusal(WaitForIdle, RestartRefusal):
    message = "Idle owner changed before restart fence."


class OwnerGenerationChangedRefusal(ChangedSelection, RestartRefusal):
    message = "Owner admission generation changed before restart."


class OwnerSelectionChangedRefusal(ChangedSelection, RestartRefusal):
    message = "Owner selection changed before restart."
