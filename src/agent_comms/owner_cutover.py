"""Declared policy at the existing all-stopped owner batch boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from .launch_runtime import LaunchRuntime

if TYPE_CHECKING:
    from .maintenance_barrier import MaintenanceState
    from .owner_lifecycle import OwnerLifecycle, OwnerRestartResult
    from .owner_restart import OwnerRestartRequest, StoppedOwnerBatch, StoppedOwnerFailure
    from .registry_document import RegistrySnapshot
    from .threads import Thread


class OwnerCutover(ABC):
    """An operation owns its audience and retained maintenance proof.

    Selection validation precedes every fence or signal. Installation runs with
    the existing wire lock held, after original process exit and before
    replacement launch. A member must not reacquire that lock or start owners.
    """

    def restart(self, lifecycle: OwnerLifecycle, request: OwnerRestartRequest) -> tuple[OwnerRestartResult, ...]:
        """The cutover member owns which runtime may decode original records."""
        from .owner_restart import AdmittedOwnerBatch

        return AdmittedOwnerBatch.restart(lifecycle, request, self)

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

    @abstractmethod
    def complete(self, stopped: StoppedOwnerBatch) -> tuple[OwnerRestartResult, ...]:
        """Only acquired all-stopped custody can install and launch a target."""
        pass

    def recover(self, stopped: StoppedOwnerBatch) -> tuple[OwnerRestartResult, ...]:
        """No recovery without this operation's unchanged-original proof."""
        from .errors import RelationViolationError

        raise RelationViolationError('This operation has not certified original-unchanged recovery')

    def failed(self, failure: StoppedOwnerFailure) -> NoReturn:
        """Transfer live custody to the caller, or dispose within this operation.

        A one-shot process member must make its disposition before it exits.
        Recovery of unchanged owners is distinct from retrying installation or
        input; the original exception remains the result in either case.
        """
        raise failure

    def leave_stopped(self, failure: StoppedOwnerFailure) -> NoReturn:
        """A one-shot operation explicitly relinquishes failed launch custody."""
        failure.abandon()
        raise failure

    def restore_unchanged(self, failure: StoppedOwnerFailure) -> NoReturn:
        """Finish a declared safe disposition before a one-shot caller exits.

        Recovery is certified by recover(), never inferred from the exception
        or its text. No installation or input is repeated. A refused recovery
        leaves the recorded operation for review and reports BOTH failures.
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
    clears that exact hold under the wire lock after known-safe publication
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
        self.bind_target_launch(stopped.lifecycle)
        return stopped.launch(self.launch_runtime(stopped))

    def launch_runtime(self, stopped: StoppedOwnerBatch) -> LaunchRuntime:
        """An installation selects the runtime after publishing its pointer."""
        return LaunchRuntime.current()

    def bind_target_launch(self, lifecycle: OwnerLifecycle) -> None:
        """Bind installed launch authority after conversion, before any launch.

        Nested installations only run after_stopped; the outer operation owns
        the final target binding. Preserving members need no new binding.
        """
        pass

    @abstractmethod
    def after_stopped(self, lifecycle: OwnerLifecycle) -> None:
        pass


class PreserveOwnerRuntime(StoppedOwnerInstallation):
    """Normal retained restart needs no wire/index installation."""

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
        # original incarnation/admission/process before the first source launch.
        return stopped.handoff.restore(stopped.lifecycle)
