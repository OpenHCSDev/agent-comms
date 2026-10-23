"""Durable attempt phases with lease/completion data owned by live and terminal states.

Persisted names remain readable even where a live producer has not yet emitted
that phase. Native turn ownership remains S2's; these are durable observations.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .coordination_errors import (
    IdentityConflict,
    IntegrityViolationError,
    RecoveryBlocked,
    ResponseClaimBlocked,
)
from .coordinator_vocabulary import CoordinatorVocabulary
from .lifecycle import LifecycleState
from .lifecycle_capability import Failed, Terminal

if TYPE_CHECKING:
    from .coordination_tables.attempts import ReplayFact


class FromPromptStartingAttempt:
    @classmethod
    def after_prompt_starting_attempt(cls) -> bool:
        return True


class FromPromptAcceptedAttempt:
    @classmethod
    def after_prompt_accepted_attempt(cls) -> bool:
        return True


class FromModelRunningAttempt:
    @classmethod
    def after_model_running_attempt(cls) -> bool:
        return True


class FromToolRunningAttempt:
    @classmethod
    def after_tool_running_attempt(cls) -> bool:
        return True


class FromCompactionAttempt:
    @classmethod
    def after_compaction_attempt(cls) -> bool:
        return True


class FromSettlingAttempt:
    @classmethod
    def after_settling_attempt(cls) -> bool:
        return True


class FromModelStalledAttempt:
    @classmethod
    def after_model_stalled_attempt(cls) -> bool:
        return True


class FromAbortingAttempt:
    @classmethod
    def after_aborting_attempt(cls) -> bool:
        return True


class FromRetryingAttempt:
    @classmethod
    def after_retrying_attempt(cls) -> bool:
        return True


class FromProviderUnavailableAttempt:
    @classmethod
    def after_provider_unavailable_attempt(cls) -> bool:
        return True



@dataclass(frozen=True)
class AttemptState(CoordinatorVocabulary, LifecycleState, affix="Attempt"):
    @classmethod
    def sql_vocabulary(cls) -> dict[str, str]:
        return dict(
            attempt_names=cls.sql_values(cls.names()),
            terminal_attempt_names=cls.sql_members(cls.members_with(Terminal)),
            failed_attempt_phases=cls.sql_members(cls.members_with(Failed)),
            retry_attempt_phases=cls.sql_members(cls.members_with(RetryPredecessor)),
        )

    def permits_retry(self) -> bool:
        return False

    def accepts_active_execution(self) -> bool:
        """Only a live attempt can support the execution's current pointer."""
        return False

    preserve_check: ClassVar[bool] = True
    allows_tool_claim: ClassVar[bool] = False
    succeeded: ClassVar[bool] = False
    starting: ClassVar[bool] = False
    running: ClassVar[bool] = False
    settling: ClassVar[bool] = False
    has_model_context: ClassVar[bool] = False

    def observed(self, phase, *, backend_done: bool, process_dead: bool, progress: bool):
        if phase is not type(self) and not self.may_become(phase):
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

    def require_silent_completion(self) -> None:
        raise IdentityConflict("silent completion requires settling phase")

    def renewed(self, expiry: int):
        if self.process_dead:
            raise RecoveryBlocked("a dead Pi RPC subprocess cannot renew its live lease")
        return replace(self, lease_expires_at_ms=max(self.lease_expires_at_ms or 0, expiry))

    @property
    def tool_claim_open(self) -> bool:
        return self.allows_tool_claim and not (self.backend_done or self.process_dead)

    def require_final_response(self) -> None:
        raise ResponseClaimBlocked()

    @property
    def publication_ready(self) -> bool:
        return False

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

    def accepts_active_execution(self) -> bool:
        return True

    @classmethod
    def load(cls, lease, done, dead):
        return cls(lease, done, dead)


class TerminalAttempt(Terminal, AttemptState):
    @property
    def replay_contribution(self) -> ReplayFact:
        from .coordination_tables.attempts import ReplayFact

        return ReplayFact.NONE

    @property
    @abstractmethod
    def release_wording(self) -> str: ...

    @abstractmethod
    def disposition(self, snapshot, attempt): ...

    preserve_check = False

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


class PromptStartingAttempt(FromRetryingAttempt, LiveAttempt):
    allows_tool_claim = True
    starting = True

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromPromptStartingAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_prompt_starting_attempt()


class PromptAcceptedAttempt(FromPromptStartingAttempt, LiveAttempt):
    allows_tool_claim = True

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromPromptAcceptedAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_prompt_accepted_attempt()


class ContextualAttempt(LiveAttempt):
    """A live phase reached only after the tracked model context was observed."""

    has_model_context = True


class ModelRunningAttempt(FromPromptAcceptedAttempt, FromToolRunningAttempt, FromCompactionAttempt, FromRetryingAttempt, ContextualAttempt):
    allows_tool_claim = True
    running = True

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromModelRunningAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_model_running_attempt()


class ToolRunningAttempt(FromModelRunningAttempt, ContextualAttempt):
    allows_tool_claim = True

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromToolRunningAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_tool_running_attempt()


class CompactionAttempt(FromModelRunningAttempt, ContextualAttempt):
    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromCompactionAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_compaction_attempt()


class SettlingAttempt(FromModelRunningAttempt, FromToolRunningAttempt, ContextualAttempt):
    def require_silent_completion(self) -> None:
        pass

    settling = True

    @property
    def publication_ready(self) -> bool:
        return self.backend_done and self.process_dead

    def require_final_response(self) -> None:
        if not self.publication_ready:
            raise ResponseClaimBlocked()

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromSettlingAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_settling_attempt()


class ModelStalledAttempt(FromPromptAcceptedAttempt, FromModelRunningAttempt, FromCompactionAttempt, FromRetryingAttempt, LiveAttempt):
    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromModelStalledAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_model_stalled_attempt()


class AbortingAttempt(FromPromptAcceptedAttempt, FromModelRunningAttempt, FromToolRunningAttempt, FromCompactionAttempt, FromModelStalledAttempt, LiveAttempt):
    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromAbortingAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_aborting_attempt()


class RetryingAttempt(FromAbortingAttempt, FromProviderUnavailableAttempt, LiveAttempt):
    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromRetryingAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_retrying_attempt()


class ProviderUnavailableAttempt(FromPromptStartingAttempt, FromModelRunningAttempt, FromRetryingAttempt, LiveAttempt):
    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return tuple(FromProviderUnavailableAttempt.__subclasses__())

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return nxt.after_provider_unavailable_attempt()


class SucceededAttempt(FromSettlingAttempt, TerminalAttempt):
    @property
    def release_wording(self) -> str:
        return "Released files after a completed turn"

    def disposition(self, snapshot, attempt):
        from .execution_states import CompletedExecution
        from .obligation_states import SilentResponse

        attempt.lifecycle.require_silent_completion()
        snapshot.require_nonpublication_response()
        return CompletedExecution(attempt.attempt_ordinal), SilentResponse()

    succeeded = True


class UnsuccessfulTerminal(FromPromptStartingAttempt, FromPromptAcceptedAttempt, FromModelRunningAttempt, FromToolRunningAttempt, FromCompactionAttempt, FromSettlingAttempt, FromModelStalledAttempt, FromAbortingAttempt, FromRetryingAttempt, FromProviderUnavailableAttempt, Failed, TerminalAttempt):
    """A terminal attempt that did not succeed, irrespective of retry authority."""

    def disposition(self, snapshot, attempt):
        from .execution_states import FailedExecution
        from .obligation_states import FailedResponse

        return FailedExecution(attempt.attempt_ordinal), FailedResponse()


class RetryPredecessor:
    """A known terminal outcome may precede an independently authorized retry."""

    def permits_retry(self) -> bool:
        return True

    def disposition(self, snapshot, attempt):
        from .execution_states import DeferredExecution
        from .obligation_states import DeferredResponse

        if snapshot.retry_authorized:
            return DeferredExecution(attempt.attempt_ordinal), DeferredResponse()
        return super().disposition(snapshot, attempt)


class AttemptFailedAttempt(RetryPredecessor, UnsuccessfulTerminal):
    @property
    def release_wording(self) -> str:
        return "Released files after a failed turn"


class UnknownOutcomeAttempt(UnsuccessfulTerminal):
    preserve_check = True

    @property
    def release_wording(self) -> str:
        return "Released files after a turn with unknown outcome; read them before editing"

    @property
    def replay_contribution(self) -> ReplayFact:
        from .coordination_tables.attempts import ReplayFact

        return ReplayFact.UNKNOWN_EFFECTS
