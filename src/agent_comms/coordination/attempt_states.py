"""Durable attempt phases with lease/completion data owned by live and terminal states.

Persisted names remain readable even where a live producer has not yet emitted
that phase. Native turn ownership remains S2's; these are durable observations.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agent_comms.coordination.errors import (
    IdentityConflict,
    CoordinationError,
    RecoveryBlocked,
    ResponseClaimBlocked,
    StaleFence,
)
from agent_comms.coordination.execution_states import (
    CompletedExecution,
    DeferredExecution,
    FailedExecution,
)
from agent_comms.coordination.obligation_states import (
    DeferredResponse,
    FailedResponse,
    SilentResponse,
)
from agent_comms.coordination.vocabulary import CoordinatorVocabulary
from agent_comms.nominal.lifecycle import LifecycleState
from agent_comms.nominal.lifecycle_capability import Failed, Terminal

if TYPE_CHECKING:
    from agent_comms.coordination.tables.attempts import ReplayFact


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

    allows_tool_claim: ClassVar[bool] = False

    def advance_on_accepted(self, advance: Callable[[type[AttemptState]], None]) -> None:
        pass

    def require_starting(self) -> None:
        raise StaleFence("full execution is not running before native send")

    def is_succeeded(self) -> bool:
        return False

    def preserves_check(self) -> bool:
        return True

    @property
    def has_model_context(self) -> bool:
        return False

    def observed(self, phase, *, backend_done: bool, process_dead: bool, progress: bool):
        if phase is not type(self) and not issubclass(phase, self.successors()):
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
        if self.lease_expires_at_ms < 0:
            raise CoordinationError("active attempt requires a lease")

    def accepts_active_execution(self) -> bool:
        return True

    @classmethod
    def load(cls, lease, done, dead):
        return cls(lease, done, dead)


class TerminalAttempt(Terminal, AttemptState):
    @property
    def replay_contribution(self) -> ReplayFact:
        from agent_comms.coordination.tables.attempts import ReplayFact

        return ReplayFact.NONE

    @property
    @abstractmethod
    def release_wording(self) -> str: ...

    @abstractmethod
    def disposition(self, snapshot, attempt): ...

    def preserves_check(self) -> bool:
        return False

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
            raise CoordinationError("terminal attempt must release lease")
        if not (done and dead):
            raise CoordinationError("terminal attempt requires final completion/death")
        return cls()


class PromptStartingAttempt(LiveAttempt):
    allows_tool_claim = True

    def advance_on_accepted(self, advance: Callable[[type[AttemptState]], None]) -> None:
        advance(PromptAcceptedAttempt)

    def require_starting(self) -> None:
        pass

    @classmethod
    def successors(cls):
        return (
            UnsuccessfulTerminal,
            PromptAcceptedAttempt,
            ProviderUnavailableAttempt,
        )


class BeforeModelRun:
    """An attempt about to run the model: it can abort, fail, run or stall."""

    __slots__ = ()

    @classmethod
    def successors(cls):
        return (
            AbortingAttempt,
            UnsuccessfulTerminal,
            ModelRunningAttempt,
            ModelStalledAttempt,
        )


class PromptAcceptedAttempt(BeforeModelRun, LiveAttempt):
    allows_tool_claim = True



class ContextualAttempt(LiveAttempt):
    """A live phase reached only after the tracked model context was observed."""

    @property
    def has_model_context(self) -> bool:
        return True


class ModelRunningAttempt(ContextualAttempt):
    allows_tool_claim = True

    @classmethod
    def successors(cls):
        return (
            AbortingAttempt,
            UnsuccessfulTerminal,
            CompactionAttempt,
            ModelStalledAttempt,
            ProviderUnavailableAttempt,
            SettlingAttempt,
            ToolRunningAttempt,
        )


class ToolRunningAttempt(ContextualAttempt):
    allows_tool_claim = True

    @classmethod
    def successors(cls):
        return (
            AbortingAttempt,
            UnsuccessfulTerminal,
            ModelRunningAttempt,
            SettlingAttempt,
        )


class CompactionAttempt(BeforeModelRun, ContextualAttempt):
    """A compaction about to run the model."""


class SettlingAttempt(ContextualAttempt):
    def require_silent_completion(self) -> None:
        pass

    @property
    def publication_ready(self) -> bool:
        return self.backend_done and self.process_dead

    def require_final_response(self) -> None:
        if not self.publication_ready:
            raise ResponseClaimBlocked()

    @classmethod
    def successors(cls):
        return (
            UnsuccessfulTerminal,
            SucceededAttempt,
        )


class ModelStalledAttempt(LiveAttempt):
    @classmethod
    def successors(cls):
        return (
            AbortingAttempt,
            UnsuccessfulTerminal,
        )


class AbortingAttempt(LiveAttempt):
    @classmethod
    def successors(cls):
        return (
            UnsuccessfulTerminal,
            RetryingAttempt,
        )


class RetryingAttempt(LiveAttempt):
    @classmethod
    def successors(cls):
        return (
            UnsuccessfulTerminal,
            ModelRunningAttempt,
            ModelStalledAttempt,
            PromptStartingAttempt,
            ProviderUnavailableAttempt,
        )


class ProviderUnavailableAttempt(LiveAttempt):
    @classmethod
    def successors(cls):
        return (
            UnsuccessfulTerminal,
            RetryingAttempt,
        )


class SucceededAttempt(TerminalAttempt):
    @property
    def release_wording(self) -> str:
        return "Released files after a completed turn"

    def disposition(self, snapshot, attempt):
        attempt.lifecycle.require_silent_completion()
        snapshot.require_nonpublication_response()
        return CompletedExecution(attempt.attempt_ordinal), SilentResponse()

    def is_succeeded(self) -> bool:
        return True


class UnsuccessfulTerminal(Failed, TerminalAttempt):
    """A terminal attempt that did not succeed, irrespective of retry authority."""

    def disposition(self, snapshot, attempt):
        return FailedExecution(attempt.attempt_ordinal), FailedResponse()


class RetryPredecessor:
    """A known terminal outcome may precede an independently authorized retry."""

    def permits_retry(self) -> bool:
        return True

    def disposition(self, snapshot, attempt):
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
        from agent_comms.coordination.tables.attempts import ReplayFact

        return ReplayFact.UNKNOWN_EFFECTS
