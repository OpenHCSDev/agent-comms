"""Turn failure precedence, presentation and uncertain delivery in one owner."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .diagnostics import FailureReason
from .native_arguments import SelectedModel

if TYPE_CHECKING:
    from .backend import TurnSession
    from .turn_output import TurnOutput


@dataclass(frozen=True)
class TurnFailure(DeclaredFamily):
    text: str
    code: ClassVar[str | None] = None
    precedence: ClassVar[int] = 0
    input_uncertain: ClassVar[bool] = False
    # A failure of the model request itself; Pi re-sending that request ends it.
    ends_with_request_retry: ClassVar[bool] = False

    def with_text(self, text: str) -> TurnFailure:
        return replace(self, text=text)

    def with_startup_diagnostics(self, stderr: str) -> TurnFailure:
        return self

    def supersedes(self, previous: TurnFailure | None) -> bool:
        return previous is None or self.precedence >= previous.precedence

    def prepare_execution_retry(self, db, execution_id: str) -> None:
        """Consume any earlier provider proof for this nonretryable failure."""
        from .coordination_tables.attempts import ReplayAssessments

        ReplayAssessments.revoke(db, execution_id)

    def after_provider_retry_exhausted(self) -> TurnFailure:
        return self

    def raise_native(self, proof, model: SelectedModel) -> None:
        """A runtime failure does not prove a completed provider request."""
        from .native_failures import NativePiUnavailable

        raise NativePiUnavailable(self.text)

    def retry_delay(self, attempt_ordinal: int) -> float:
        raise TypeError("Only a retryable provider failure has a retry delay")


class InputIdUnavailable(TurnFailure):
    def with_startup_diagnostics(self, stderr: str) -> TurnFailure:
        return self.with_text(self.text + "\nBackend startup reported:\n" + stderr)

    code = FailureReason.INPUT_ID_UNAVAILABLE
    precedence = 100


class IdentityUncertain(TurnFailure):
    code = FailureReason.IDENTITY_UNCERTAIN
    precedence = 80
    input_uncertain = True


class AuthorityChanged(TurnFailure):
    code = FailureReason.AUTHORITY_CHANGED
    precedence = 70
    input_uncertain = True


class FollowupUnrecognized(TurnFailure):
    code = FailureReason.FOLLOWUP_UNRECOGNIZED
    precedence = 60
    input_uncertain = True


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
    code = FailureReason.INPUT_MISSING
    precedence = 50
    input_uncertain = True
    default_text = "Pi RPC run ended without this prompt's user message start."

    @classmethod
    def detected(cls, session: TurnSession, transport_ok: bool) -> bool:
        return not session.prompt_state.started and bool(
            transport_ok or session.inputs.uncertain or session.output.failure_text
        )


class FinalStopMissing(TerminalFailure):
    code = FailureReason.FINAL_STOP_MISSING
    precedence = 40
    input_uncertain = True
    default_text = "Pi RPC run ended without an authoritative final assistant stop."

    @classmethod
    def detected(cls, session: TurnSession, transport_ok: bool) -> bool:
        return transport_ok and not session.output.final_assistant_stop

    @classmethod
    def explanation(cls, output: TurnOutput) -> str:
        return output.failure_text or output.error_message or cls.default_text


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

    code = FailureReason.MODEL_REQUEST_FAILED
    precedence = 45
    ends_with_request_retry = True


class RetryableProviderFailure:
    """Capability shared by declared transient provider failures."""

    max_retries: ClassVar[int] = 3

    def prepare_execution_retry(self, db, execution_id: str) -> None:
        """A provider refusal may retry only while durable output facts are absent."""
        from .coordination_tables.attempts import ReplayAssessments

        ReplayAssessments.authorize_if_unobserved(db, execution_id)

    def retry_delay(self, attempt_ordinal: int) -> float:
        """Bounded exponential delay between distinct durable attempts."""
        return min(2 ** (attempt_ordinal - 1), 8)


class ProviderRetryExhausted(RetryableProviderFailure, ModelRequestFailed):
    """The native provider-retry owner exhausted a declared transient failure."""


class ProviderTransportFailed(RetryableProviderFailure, ModelRequestFailed):
    """The native diagnostic declared a provider transport failure."""


class QueuedInputMissing(TerminalFailure):
    code = FailureReason.QUEUED_INPUT_MISSING
    precedence = 30
    input_uncertain = True
    default_text = "Pi RPC run ended with an unstarted queued input; delivery is uncertain."

    @classmethod
    def detected(cls, session: TurnSession, transport_ok: bool) -> bool:
        return transport_ok and session.output.error_message is None and session.inputs.unresolved


class ModelStalled(TurnFailure):
    input_uncertain = True


class PromptSendFailed(TurnFailure):
    pass


class ExtensionUiFailed(TurnFailure):
    pass


class BackendDidNotExit(TurnFailure):
    pass
