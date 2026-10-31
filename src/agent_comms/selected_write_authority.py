"""A selected operator plan remains bound to its original live ACP controller."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from .field_codec import ClaimId

if TYPE_CHECKING:
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
    def writing(
        self, assignment: WakeAssignment, owner: Thread, operation_id: ClaimId
    ) -> AbstractContextManager[None]:
        """The write's whole span: current on entry, applied on success, unbound on every end."""


