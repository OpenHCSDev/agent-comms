"""Declared policy at the existing all-stopped owner batch boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from .errors import RelationViolationError
from .launch_runtime import LaunchRuntime

if TYPE_CHECKING:
    from .maintenance_barrier import MaintenanceState
    from .owner_lifecycle import OwnerLifecycle, OwnerRestartResult, OwnerRestartSelection
    from .owner_restart import OwnerRestartRequest, StoppedOwnerBatch, StoppedOwnerFailure
    from .registry_document import RegistrySnapshot
    from .threads import Thread


class OwnerCutover(ABC):
    """An operation owns its audience and retained maintenance proof.

    Selection validation precedes every fence or signal. Installation runs
    after original process exit and before replacement launch. Each replacement
    reserves its exact stopped declaration in the registry transaction.
    """

    def restart(self, lifecycle: OwnerLifecycle, request: OwnerRestartRequest) -> tuple[OwnerRestartResult, ...]:
        """The cutover member owns which runtime may decode original records."""
        from .owner_restart import ClaimedOwnerBatch

        return ClaimedOwnerBatch.restart(lifecycle, request, self)

    def owner_selection(
        self, snapshot: RegistrySnapshot, owner: Thread, request: OwnerRestartRequest,
    ) -> OwnerRestartSelection:
        return request.owner_selection(snapshot, owner)

    @abstractmethod
    def require_selection(self, snapshot: RegistrySnapshot, owners: Sequence[Thread]) -> None:
        pass

    def require_launch(self, lifecycle: OwnerLifecycle) -> None:
        """Installations may supply target launch authority only after stopping."""
        pass

    def require_restart_phase(self, lifecycle: OwnerLifecycle) -> None:
        """Ordinary restarts require READY; maintained cutovers bind their hold."""
        from .restart_refusals import OwnerMaintenancePendingRefusal

        if lifecycle.maintenance.current_unlocked().turn_pending:
            raise OwnerMaintenancePendingRefusal()

    def stopped_resources(self, lifecycle: OwnerLifecycle):
        """Acquire installation resources after the selected readers exit."""
        return nullcontext()

    @abstractmethod
    def complete(self, stopped: StoppedOwnerBatch) -> tuple[OwnerRestartResult, ...]:
        """Install and launch only from a batch whose owners have all stopped."""
        pass

    def recover(self, stopped: StoppedOwnerBatch) -> tuple[OwnerRestartResult, ...]:
        """No recovery without this operation's unchanged-original proof."""
        raise RelationViolationError('This operation has not checked original-unchanged recovery')

    def failed(self, failure: StoppedOwnerFailure) -> NoReturn:
        """Release the operation's resources before reporting its failure.

        Call restore_unchanged instead when the same processes can safely be
        restored. Restoring them does not retry installation or input, and
        either path still reports the exception that caused the failure.
        """
        self.leave_stopped(failure)

    def leave_stopped(self, failure: StoppedOwnerFailure) -> NoReturn:
        """A one-shot operation explicitly releases its failed-launch resources."""
        failure.abandon()
        raise failure

    def restore_unchanged(self, failure: StoppedOwnerFailure) -> NoReturn:
        """Try the operation's recovery method before its caller exits.

        Let recover() decide whether recovery succeeded; do not infer success
        from exception text. Do not repeat installation or input. If recovery
        fails, keep the operation for review and report both failures.
        """
        try:
            failure.recover()
        except BaseException as recovery_error:
            failure.add_note(f'Original-runtime recovery refused or failed: {recovery_error!r}')
            self.leave_stopped(failure)
        raise failure


class MaintainedOwnerCutover(OwnerCutover):
    """Stop an exact fleet while its durable Draining state holds new turns.

    The publisher obtains maintenance_hold before polling idle owners. It
    clears that exact hold under the registry lock after known-safe publication
    (or on a known-safe abort), before any replacement launch. UNKNOWN outcomes
    remain closed for explicit review rather than reopening on exception text.
    """

    maintenance_hold: MaintenanceState

    def require_restart_phase(self, lifecycle: OwnerLifecycle) -> None:
        lifecycle.maintenance.require_draining_unlocked(self.maintenance_hold)


class StoppedOwnerInstallation(OwnerCutover):
    """A same-format operation needs no cross-runtime completion transfer."""

    def recovery_paths(self) -> frozenset[Path]:
        """Original file destinations whose change forbids source restoration."""
        return frozenset()

    def complete(self, stopped: StoppedOwnerBatch) -> tuple[OwnerRestartResult, ...]:
        self.after_stopped(stopped.lifecycle)
        return stopped.launch(self.launch_runtime(stopped))

    def launch_runtime(self, stopped: StoppedOwnerBatch) -> LaunchRuntime:
        """Only installation selects the runtime after publishing its pointer."""
        selected = LaunchRuntime.installed()
        selected.require_available()
        return selected

    @abstractmethod
    def after_stopped(self, lifecycle: OwnerLifecycle) -> None:
        pass


class PreserveOwnerRuntime(StoppedOwnerInstallation):
    """Normal retained restart needs no installation."""

    def launch_runtime(self, stopped: StoppedOwnerBatch) -> LaunchRuntime:
        return stopped.handoff.launch_runtime

    def require_selection(self, snapshot: RegistrySnapshot, owners: Sequence[Thread]) -> None:
        pass

    def require_launch(self, lifecycle: OwnerLifecycle) -> None:
        # This operation installs nothing and cannot repair missing authority.
        lifecycle.require_launch_authority()

    def after_stopped(self, lifecycle: OwnerLifecycle) -> None:
        pass

    def recover(self, stopped: StoppedOwnerBatch) -> tuple[OwnerRestartResult, ...]:
        # This member installs nothing. The shared handoff revalidates EVERY
        # captured thread identity, input generation and process before any launch.
        return stopped.handoff.restore(stopped.lifecycle)
