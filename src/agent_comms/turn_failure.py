"""Turn failure precedence, presentation and uncertain delivery in one owner."""

from __future__ import annotations

import signal
from abc import abstractmethod
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .errors import StaleAttemptError
from .native_arguments import SelectedModel
from .native_failures import NativePiUnavailable

if TYPE_CHECKING:
    from .backend import TurnSession
    from .goal_attempts import GoalAttemptStore, LaunchPermit
    from .goal_failure_observation import FailedTurnObservation
    from .turn_output import TurnOutput


@dataclass(frozen=True)
class TurnFailure(DeclaredFamily):
    text: str
    # A failure of the model request itself; Pi re-sending that request ends it.
    ends_with_request_retry: ClassVar[bool] = False

    def with_text(self, text: str) -> TurnFailure:
        return replace(self, text=text)

    def with_startup_diagnostics(self, stderr: str) -> TurnFailure:
        return self

    def supersedes(self, previous: TurnFailure | None) -> bool:
        return previous is None or self.declared_position() < previous.declared_position()

    def prepare_execution_retry(self, db, execution_id: str) -> None:
        """Consume any earlier provider proof for this nonretryable failure."""
        from .coordination_tables.executions import ReplayAssessments

        ReplayAssessments.revoke(db, execution_id)

    def after_provider_retry_exhausted(self) -> TurnFailure:
        return self

    def raise_native(self, proof, model: SelectedModel) -> None:
        """A runtime failure does not prove a completed provider request."""
        raise NativePiUnavailable(self.text)

    def retry_delay(self, attempt_ordinal: int) -> float:
        raise TypeError("Only a retryable provider failure has a retry delay")

    @classmethod
    def settle_attempt(
        cls,
        permit: LaunchPermit,
        store: GoalAttemptStore,
        observation: FailedTurnObservation,
    ) -> None:
        """A failed attempt blocks its goal until someone retries it."""
        from .attempt_resolution import FailedTurnResolution

        with suppress(StaleAttemptError):
            permit.reservation.fail(store, FailedTurnResolution(cls), observation=observation)


class InputIdUnavailable(TurnFailure):
    def with_startup_diagnostics(self, stderr: str) -> TurnFailure:
        return self.with_text(self.text + "\nBackend startup reported:\n" + stderr)


class NativePreflightTimeout(InputIdUnavailable):
    """Input-ID preflight expired before the native child attested."""


class NativePreflightExit(InputIdUnavailable):
    """The native child exited before input-ID preflight attested."""


class IdentityUncertain(TurnFailure):
    pass


class AuthorityChanged(TurnFailure):
    pass


class FollowupUnrecognized(TurnFailure):
    pass


class TerminalFailure(TurnFailure):
    """Missing terminal evidence, discovered from the same declaration as its verdict."""

    default_text: ClassVar[str]

    @classmethod
    @abstractmethod
    def detected(cls, session: TurnSession, transport_ok: bool) -> bool: ...

    @classmethod
    def explanation(cls, output: TurnOutput) -> str:
        return output.failure_text or cls.default_text


class InputMissing(TerminalFailure):
    default_text = "Pi RPC run ended without this prompt's user message start."

    @classmethod
    def detected(cls, session: TurnSession, transport_ok: bool) -> bool:
        return not session.prompt_state.started and bool(
            transport_ok or session.inputs.uncertain or session.output.failure_text
        )


class ModelRequestFailed(TurnFailure):
    """An original native error terminal is evidence, not a missing stop."""

    @classmethod
    def from_diagnostics(cls, text: str, diagnostics) -> TurnFailure:
        failure = cls(text)
        for diagnostic in diagnostics:
            failure = diagnostic.refine_model_request_failure(failure)
        return failure

    def after_provider_retry_exhausted(self) -> TurnFailure:
        return ProviderRetryExhausted(self.text)

    def raise_native(self, proof, model: SelectedModel) -> None:
        from .native_pi import NativePiTerminalFailure

        raise NativePiTerminalFailure(self.text, proof, model, self)

    ends_with_request_retry = True


class RetryableProviderFailure:
    """Capability shared by declared transient provider failures."""

    max_retries: ClassVar[int] = 3

    def prepare_execution_retry(self, db, execution_id: str) -> None:
        """A provider refusal may retry only while durable output facts are absent."""
        from .coordination_tables.executions import ReplayAssessments

        ReplayAssessments.authorize_if_unobserved(db, execution_id)

    def retry_delay(self, attempt_ordinal: int) -> float:
        """Bounded exponential delay between distinct durable attempts."""
        return min(2 ** (attempt_ordinal - 1), 8)


class ProviderRetryExhausted(RetryableProviderFailure, ModelRequestFailed):
    """The native provider-retry owner exhausted a declared transient failure."""


class ProviderTransportFailed(RetryableProviderFailure, ModelRequestFailed):
    """The native diagnostic declared a provider transport failure."""


class FinalStopMissing(TerminalFailure):
    default_text = "Pi RPC run ended without an authoritative final assistant stop."

    @classmethod
    def detected(cls, session: TurnSession, transport_ok: bool) -> bool:
        return transport_ok and not session.output.final_assistant_stop

    @classmethod
    def explanation(cls, output: TurnOutput) -> str:
        return output.failure_text or output.error_message or cls.default_text


class QueuedInputMissing(TerminalFailure):
    default_text = "Pi RPC run ended with an unstarted queued input; delivery is uncertain."

    @classmethod
    def detected(cls, session: TurnSession, transport_ok: bool) -> bool:
        return transport_ok and session.output.error_message is None and session.inputs.unresolved


class ModelStalled(TurnFailure):
    pass


class PromptSendFailed(TurnFailure):
    pass


class ExtensionUiFailed(TurnFailure):
    pass


class BackendDidNotExit(TurnFailure):
    pass


class BackendFailed(TurnFailure):
    """A failed terminal without a more specific native failure declaration."""


class NativeLaunchInvalid(BackendFailed):
    """The configured native launch could not be acquired."""


class ResumableFailure:
    """Capability: the turn ended without its goal failing; the next turn resumes the goal."""

    @classmethod
    def settle_attempt(
        cls,
        permit: LaunchPermit,
        store: GoalAttemptStore,
        observation: FailedTurnObservation,
    ) -> None:
        permit.stop(store)


class CoordinationRefused(ResumableFailure, TurnFailure):
    """The bus refused the turn's own read or write (a stale fence, a coverage deadline,
    a busy store): the refusal protects the bus and says nothing about the backend."""


class NativeChildSignaled(ResumableFailure, TurnFailure):
    """A signal ended the native child: the stop an operator gives, never a backend failure.

    Declared last, so any failure the turn already recorded keeps precedence.
    """

    # Pi's rpc-mode shutdown exits 128 + SIGTERM or 128 + SIGHUP (rpc-mode.js:296).
    stop_exits: ClassVar[frozenset[int]] = frozenset({128 + signal.SIGTERM, 128 + signal.SIGHUP})
