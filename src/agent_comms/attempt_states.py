"""Durable attempt phases with lease/completion data owned by live and terminal states.

Persisted names remain readable even where a live producer has not yet emitted
that phase. Native turn ownership remains S2's; these are durable observations.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import ClassVar

from .coordination_errors import IntegrityViolationError, IdentityConflict, RecoveryBlocked
from .declared_family import DeclaredFamily
from .lifecycle import LifecycleState


@dataclass(frozen=True)
class AttemptState(DeclaredFamily, LifecycleState, affix="Attempt"):
    terminal: ClassVar[bool] = False
    allows_tool_admission: ClassVar[bool] = False
    succeeded: ClassVar[bool] = False
    failed: ClassVar[bool] = False
    starting: ClassVar[bool] = False
    running: ClassVar[bool] = False
    settling: ClassVar[bool] = False

    def observed(self, phase, *, backend_done: bool, process_dead: bool, progress: bool):
        if phase is not type(self) and phase not in self.successors():
            raise IdentityConflict("attempt phase edge is not declared")
        if self.process_dead or self.backend_done:
            # Once either finality fact is recorded, the backend cannot
            # emit another phase or progress observation.  The other fact
            # may arrive later on the SAME phase before atomic settlement.
            new_final_fact = (self.backend_done, self.process_dead) != (
                self.backend_done or backend_done,
                self.process_dead or process_dead,
            )
            if phase is not type(self) or progress or not new_final_fact:
                raise RecoveryBlocked("final backend evidence forbids further phase or progress")
        return phase.load(
            self.lease_expires_at_ms,
            self.backend_done or backend_done,
            self.process_dead or process_dead,
        )

    def renewed(self, expiry: int):
        if self.process_dead:
            raise RecoveryBlocked("a dead Pi RPC subprocess cannot renew its live lease")
        return replace(self, lease_expires_at_ms=max(self.lease_expires_at_ms or 0, expiry))

    @property
    def tool_admission_open(self) -> bool:
        return self.allows_tool_admission and not (self.backend_done or self.process_dead)

    @property
    def publication_ready(self) -> bool:
        return False

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[AttemptState], ...]: ...

    @classmethod
    @abstractmethod
    def load(cls, lease: int | None, done: bool, dead: bool) -> AttemptState: ...


@dataclass(frozen=True)
class LiveAttempt(AttemptState):
    lease_expires_at_ms: int
    backend_done: bool = False
    process_dead: bool = False

    def __post_init__(self):
        if type(self.lease_expires_at_ms) is not int or self.lease_expires_at_ms < 0:
            raise IntegrityViolationError("active attempt requires a lease")

    @classmethod
    def load(cls, lease, done, dead):
        return cls(lease, done, dead)


class TerminalAttempt(AttemptState):
    @abstractmethod
    def disposition(self, snapshot, attempt): ...

    terminal = True

    @property
    def lease_expires_at_ms(self):
        return None

    @property
    def backend_done(self):
        return True

    @property
    def process_dead(self):
        return True

    @classmethod
    def load(cls, lease, done, dead):
        if lease is not None:
            raise IntegrityViolationError("terminal attempt must release lease")
        if not (done and dead):
            raise IntegrityViolationError("terminal attempt requires final completion/death")
        return cls()


class PromptStartingAttempt(LiveAttempt):
    allows_tool_admission = True
    starting = True

    @classmethod
    def successors(cls):
        return (
            AttemptFailedAttempt,
            PromptAcceptedAttempt,
            ProviderUnavailableAttempt,
        )


class PromptAcceptedAttempt(LiveAttempt):
    allows_tool_admission = True

    @classmethod
    def successors(cls):
        return (
            AbortingAttempt,
            AttemptFailedAttempt,
            ModelRunningAttempt,
            ModelStalledAttempt,
        )


class ModelRunningAttempt(LiveAttempt):
    allows_tool_admission = True
    running = True

    @classmethod
    def successors(cls):
        return (
            AbortingAttempt,
            AttemptFailedAttempt,
            CompactionAttempt,
            ModelStalledAttempt,
            ProviderUnavailableAttempt,
            SettlingAttempt,
            ToolRunningAttempt,
        )


class ToolRunningAttempt(LiveAttempt):
    allows_tool_admission = True

    @classmethod
    def successors(cls):
        return (
            AbortingAttempt,
            AttemptFailedAttempt,
            ModelRunningAttempt,
            SettlingAttempt,
        )


class CompactionAttempt(LiveAttempt):
    @classmethod
    def successors(cls):
        return (
            AbortingAttempt,
            AttemptFailedAttempt,
            ModelRunningAttempt,
            ModelStalledAttempt,
        )


class SettlingAttempt(LiveAttempt):
    settling = True

    @property
    def publication_ready(self) -> bool:
        return self.backend_done and self.process_dead

    @classmethod
    def successors(cls):
        return (
            AttemptFailedAttempt,
            SucceededAttempt,
        )


class ModelStalledAttempt(LiveAttempt):
    @classmethod
    def successors(cls):
        return (
            AbortingAttempt,
            AttemptFailedAttempt,
        )


class AbortingAttempt(LiveAttempt):
    @classmethod
    def successors(cls):
        return (
            AttemptFailedAttempt,
            RetryingAttempt,
        )


class RetryingAttempt(LiveAttempt):
    @classmethod
    def successors(cls):
        return (
            AttemptFailedAttempt,
            ModelRunningAttempt,
            ModelStalledAttempt,
            PromptStartingAttempt,
            ProviderUnavailableAttempt,
        )


class ProviderUnavailableAttempt(LiveAttempt):
    @classmethod
    def successors(cls):
        return (
            AttemptFailedAttempt,
            RetryingAttempt,
        )


class SucceededAttempt(TerminalAttempt):
    def disposition(self, snapshot, attempt):
        from .execution_states import CompletedExecution
        from .obligation_states import SilentResponse

        if not attempt.lifecycle.settling:
            raise IdentityConflict("silent completion requires settling phase")
        snapshot.require_nonpublication_response()
        return CompletedExecution(attempt.attempt_ordinal), SilentResponse()

    succeeded = True

    @classmethod
    def successors(cls):
        return ()


class AttemptFailedAttempt(TerminalAttempt):
    def disposition(self, snapshot, attempt):
        from .execution_states import DeferredExecution, FailedExecution
        from .obligation_states import DeferredResponse, FailedResponse

        if snapshot.retry_authorized:
            return DeferredExecution(attempt.attempt_ordinal), DeferredResponse()
        return FailedExecution(attempt.attempt_ordinal), FailedResponse()

    failed = True

    @classmethod
    def successors(cls):
        return ()
