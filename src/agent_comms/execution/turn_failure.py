"""Turn failure precedence, presentation and uncertain delivery in one owner."""


import re
import signal
from abc import abstractmethod
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, replace
from sqlite3 import Connection
from typing import ClassVar

from agent_comms.errors import StaleAttemptError
from agent_comms.execution.native_arguments import SelectedModel
from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.turn_failure_reads import (
    GoalFailurePermit,
    ModelFailureDiagnostic,
    ReplayPreparation,
    TerminalInputsRead,
    TerminalOutputRead,
    TerminalSessionRead,
)
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.planning.attempt_resolution import RecordedResolution


@dataclass(frozen=True)
class TurnFailure(DeclaredFamily):
    text: str
    # A failure of the model request itself; Pi re-sending that request ends it.
    ends_with_request_retry: ClassVar[bool] = False
    # Whether a turn ending with this failure is clean; only the absent failure is.
    clean: ClassVar[bool] = False

    def with_text(self, text: str) -> TurnFailure:
        return replace(self, text=text)

    def with_startup_diagnostics(self, stderr: str) -> TurnFailure:
        return self

    def supersedes(self, previous: TurnFailure) -> bool:
        return previous.superseded_by(self)

    def superseded_by(self, failure: TurnFailure) -> bool:
        return failure.declared_position() < self.declared_position()

    def terminal_text(self, otherwise: Callable[[], str]) -> str:
        """A declared failure ends the turn with its own text."""
        return self.text

    def declared_or(self, undeclared: TurnFailure) -> TurnFailure:
        return self

    def prepare_execution_retry(
        self, db: Connection, execution_id: str, replay: type[ReplayPreparation],
    ) -> None:
        """Consume any earlier provider proof for this nonretryable failure."""
        replay.revoke(db, execution_id)

    def after_provider_retry_exhausted(self) -> TurnFailure:
        return self

    def raise_native(self, proof: object, model: SelectedModel) -> None:
        """A runtime failure does not prove a completed provider request."""
        raise NativePiUnavailable(self.text)

    def retry_delay(self, attempt_ordinal: int) -> float:
        raise TypeError("Only a retryable provider failure has a retry delay")

    @classmethod
    def settle_attempt[Store, Observation](
        cls, permit: GoalFailurePermit[Store, Observation], store: Store,
        observation: Observation,
    ) -> None:
        """A failed attempt blocks its goal until someone retries it."""
        with suppress(StaleAttemptError):
            permit.reservation.fail(store, FailedTurnResolution(cls), observation=observation)


class NativePiTerminalFailure[Proof](NativePiUnavailable):
    """A proved input ended in a failed terminal and its owned process was reaped."""

    def __init__(
        self, detail: str, context: Proof, model: SelectedModel,
        failure: TurnFailure,
    ) -> None:
        super().__init__(f"Native Pi assistant did not finish successfully: {detail}")
        self.context = context
        self.model = model
        self.failure = failure
        # Provider text may contain arbitrary response bodies. Only complete
        # known short errors may be broadcast into a shared channel.
        match = re.fullmatch(
            r"(?:(?:Codex|OpenAI|Anthropic) error: )?"
            r"(The usage limit has been reached|Insufficient credits|"
            r"(?:401|402|403|429)(?::\s*|\s+)(?:insufficient credits|rate limit|unauthorized)|"
            r"Model output limit reached)\.?",
            detail,
            flags=re.IGNORECASE,
        )
        self.public_message = (
            f"{model.qualified}: {match.group(1) if match else 'request failed'}."
        )


@dataclass(frozen=True)
class FailedTurnResolution(RecordedResolution):
    failure: type[TurnFailure]


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
    def detected(cls, session: TerminalSessionRead, transport_ok: bool) -> bool: ...

    @classmethod
    def explanation(cls, output: TerminalOutputRead) -> str:
        return output.failure_text or cls.default_text


class InputMissing(TerminalFailure):
    default_text = "Pi RPC run ended without this prompt's user message start."

    @classmethod
    def detected(cls, session: TerminalSessionRead, transport_ok: bool) -> bool:
        return not session.prompt_state.started and bool(
            transport_ok or session.inputs.uncertain or session.output.failure_text
        )


class ModelRequestFailed(TurnFailure):
    """An original native error terminal is evidence, not a missing stop."""

    @classmethod
    def from_diagnostics(
        cls, text: str, diagnostics: Iterable[ModelFailureDiagnostic[TurnFailure]],
    ) -> TurnFailure:
        failure: TurnFailure = cls(text)
        for diagnostic in diagnostics:
            failure = diagnostic.refine_model_request_failure(failure)
        return failure

    def after_provider_retry_exhausted(self) -> TurnFailure:
        return ProviderRetryExhausted(self.text)

    def raise_native[Proof](self, proof: Proof, model: SelectedModel) -> None:
        raise NativePiTerminalFailure[Proof](self.text, proof, model, self)

    ends_with_request_retry = True


class RetryableProviderFailure:
    """Capability shared by declared transient provider failures."""

    max_retries: ClassVar[int] = 3

    def prepare_execution_retry(
        self, db: Connection, execution_id: str, replay: type[ReplayPreparation],
    ) -> None:
        """A provider refusal may retry only while durable output facts are absent."""
        replay.authorize_if_unobserved(db, execution_id)

    def retry_delay(self, attempt_ordinal: int) -> float:
        """Bounded exponential delay between distinct durable attempts."""
        delay: float = 2 ** (attempt_ordinal - 1)
        return min(delay, 8)


class ProviderRetryExhausted(RetryableProviderFailure, ModelRequestFailed):
    """The native provider-retry owner exhausted a declared transient failure."""


class ProviderTransportFailed(RetryableProviderFailure, ModelRequestFailed):
    """The native diagnostic declared a provider transport failure."""


class FinalStopMissing(TerminalFailure):
    default_text = "Pi RPC run ended without an authoritative final assistant stop."

    @classmethod
    def detected(cls, session: TerminalSessionRead, transport_ok: bool) -> bool:
        return transport_ok and not session.output.final_assistant_stop

    @classmethod
    def explanation(cls, output: TerminalOutputRead) -> str:
        return output.failure_text or output.error_message or cls.default_text


class QueuedInputFailure(TerminalFailure, declared_name=None):
    """A clean native terminal still needs evidence for each queued user input."""

    @classmethod
    def detected(cls, session: TerminalSessionRead, transport_ok: bool) -> bool:
        return transport_ok and session.output.clean and cls.pending(session.inputs)

    @classmethod
    @abstractmethod
    def pending(cls, inputs: TerminalInputsRead) -> bool: ...


class QueuedInputMissing(QueuedInputFailure):
    default_text = "Pi RPC run ended with a written input that never started; delivery is uncertain."

    @classmethod
    def pending(cls, inputs: TerminalInputsRead) -> bool:
        return bool(inputs.pending)


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
    def settle_attempt[Store, Observation](
        cls, permit: GoalFailurePermit[Store, Observation], store: Store,
        observation: Observation,
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


@dataclass(frozen=True)
class NoTurnFailure(TurnFailure):
    """The turn has not failed: every declared failure supersedes it, and it raises nothing."""

    text: str = ""
    clean: ClassVar[bool] = True

    def superseded_by(self, failure: TurnFailure) -> bool:
        return True

    def raise_native(self, proof: object, model: SelectedModel) -> None:
        """Nothing failed, so nothing is raised."""

    def terminal_text(self, otherwise: Callable[[], str]) -> str:
        return otherwise()

    def declared_or(self, undeclared: TurnFailure) -> TurnFailure:
        return undeclared
