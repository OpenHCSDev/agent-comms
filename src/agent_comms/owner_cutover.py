"""Declared policy at the existing all-stopped owner batch boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .owner_lifecycle import OwnerLifecycle
    from .registry_document import RegistrySnapshot
    from .threads import Thread


class OwnerCutover(ABC):
    """An operation owns its audience and retained maintenance proof.

    Selection validation precedes every fence or signal. Installation runs with
    the existing wire admission lock held, after original process exit and before
    replacement launch. A member must not reacquire that lock or start owners.
    """

    @abstractmethod
    def require_selection(self, snapshot: RegistrySnapshot, owners: Sequence[Thread]) -> None:
        pass

    @abstractmethod
    def after_stopped(self, lifecycle: OwnerLifecycle) -> None:
        pass


class PreserveOwnerRuntime(OwnerCutover):
    """Normal retained restart needs no wire/index installation."""

    def require_selection(self, snapshot: RegistrySnapshot, owners: Sequence[Thread]) -> None:
        pass

    def after_stopped(self, lifecycle: OwnerLifecycle) -> None:
        pass
