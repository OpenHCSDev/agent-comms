"""Declared policy at the existing all-stopped owner batch boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from .owner_lifecycle import OwnerLifecycle
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .owner_restart import OwnerRestartRequest, StoppedOwnerBatch, StoppedOwnerFailure
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


class StoppedOwnerInstallation(OwnerCutover):
    """A same-format operation needs no cross-runtime completion transfer."""

    def recovery_paths(self) -> frozenset[Path]:
        """Original file destinations whose change forbids source restoration."""
        return frozenset()

    def complete(self, stopped: StoppedOwnerBatch) -> tuple[OwnerRestartResult, ...]:
        self.after_stopped(stopped.lifecycle)
        self.bind_target_launch(stopped.lifecycle)
        return stopped.launch()

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

    def require_selection(self, snapshot: RegistrySnapshot, owners: Sequence[Thread]) -> None:
        pass

    def after_stopped(self, lifecycle: OwnerLifecycle) -> None:
        pass

    def recover(self, stopped: StoppedOwnerBatch) -> tuple[OwnerRestartResult, ...]:
        # This member installs nothing. The shared handoff revalidates EVERY
        # original incarnation/admission/process before the first source launch.
        return stopped.handoff.restore(stopped.lifecycle)
