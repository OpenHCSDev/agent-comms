"""Declared policy at the existing all-stopped owner batch boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .owner_lifecycle import OwnerLifecycle
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .owner_restart import OwnerRestartRequest, StoppedOwnerBatch
    from .owner_lifecycle import OwnerRestartResult


class OwnerCutover(ABC):
    """An operation owns its audience and retained maintenance proof.

    Selection validation precedes every fence or signal. Installation runs with
    the existing wire admission lock held, after original process exit and before
    replacement launch. A member must not reacquire that lock or start owners.
    """

    def restart(self, lifecycle: OwnerLifecycle, request: OwnerRestartRequest) -> tuple[OwnerRestartResult, ...]:
        """The admission member owns which runtime may decode original records."""
        from .owner_restart import AdmittedOwnerBatch

        return AdmittedOwnerBatch.restart(lifecycle, request, self)

    @abstractmethod
    def require_selection(self, snapshot: RegistrySnapshot, owners: Sequence[Thread]) -> None:
        pass

    @abstractmethod
    def complete(self, stopped: StoppedOwnerBatch) -> tuple[OwnerRestartResult, ...]:
        """Only acquired all-stopped custody can install and launch a target."""
        pass


class StoppedOwnerInstallation(OwnerCutover):
    """A same-format operation needs no cross-runtime completion transfer."""

    def complete(self, stopped: StoppedOwnerBatch) -> tuple[OwnerRestartResult, ...]:
        self.after_stopped(stopped.lifecycle)
        return stopped.launch()

    @abstractmethod
    def after_stopped(self, lifecycle: OwnerLifecycle) -> None:
        pass


class PreserveOwnerRuntime(StoppedOwnerInstallation):
    """Normal retained restart needs no wire/index installation."""

    def require_selection(self, snapshot: RegistrySnapshot, owners: Sequence[Thread]) -> None:
        pass

    def after_stopped(self, lifecycle: OwnerLifecycle) -> None:
        pass
