from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms import agent_events as events
from agent_comms.execution.acp_failure import (
    FailureSource,
    NoRetry,
    ProviderRetryResult,
    UnrecordedFailureSource,
)
from agent_comms.execution.native_arguments import SelectedModel
from agent_comms.execution.native_session_callbacks import (
    NativeSessionCallbacks,
    SessionOutputCallbacks,
)
from agent_comms.execution.pi_wire import PiDiagnostic
from agent_comms.execution.turn_failure import NoTurnFailure, TurnFailure
from agent_comms.execution.turn_failure_reads import TerminalOutputRead
from agent_comms.pi_payloads import AssistantMessage, NativeMessageFailureSource


@dataclass
class TurnOutput(SessionOutputCallbacks):
    sensitive: bool = False
    failure: TurnFailure = field(default_factory=NoTurnFailure)
    error_message: str | None = None
    _failed_message_source: FailureSource = field(
        default_factory=UnrecordedFailureSource, init=False, repr=False)
    final_assistant_stop: bool = False
    diagnostic: dict[str, int] = field(default_factory=dict)
    # The live turn owns the one privacy-safe decoded retry result. Completion
    # and failure presentation retain this immutable value; they do not decode it.
    retry_result: ProviderRetryResult = field(default_factory=NoRetry)
    # The text since the last tool round or later input, and the current message's text.
    text_parts: list[str] = field(default_factory=list, repr=False)
    message_parts: list[str] = field(default_factory=list, repr=False)

    def append(self, delta: str) -> None:
        self.text_parts.append(delta)
        self.message_parts.append(delta)

    def start_message(self) -> None:
        self.message_parts.clear()

    def matches_message(self, text: str) -> bool:
        return bool(text) and text == "".join(self.message_parts)

    def discard_text(self) -> None:
        self.text_parts.clear()

    def interrupted(self) -> None:
        self._failed_message_source = UnrecordedFailureSource()
        self.discard_text()
        self.error_message = None
        self.final_assistant_stop = False

    def record_native_failure(self, message: AssistantMessage) -> None:
        self._failed_message_source = NativeMessageFailureSource(message)

    @property
    def native_failure_source(self) -> NativeMessageFailureSource:
        return self._failed_message_source.require_native()

    def record_failure(self, failure: TurnFailure) -> None:
        if failure.supersedes(self.failure):
            self.failure = failure.with_text(self.redact(failure.text))

    def _end_request_failure(self) -> None:
        """The refused request's failure no longer decides the turn."""
        self.failure.end_request_failure(self)

    def request_retried(self) -> None:
        """Pi re-sends the refused request; that refusal no longer decides the turn."""
        self._failed_message_source = UnrecordedFailureSource()
        self._end_request_failure()
        self.error_message = None

    def request_exhausted(self, failure: TurnFailure) -> None:
        """Pi's recovery ended the request; its final error replaces the interim refusal."""
        self._end_request_failure()
        self.record_failure(failure)

    def raise_if_failed(self, proof, model: SelectedModel) -> None:
        self.declared_failure.raise_native(proof, model)

    @property
    def declared_failure(self) -> TurnFailure:
        return self.retry_result.refine_failure(self.failure)

    @property
    def failure_text(self) -> str:
        return self.failure.text

    @property
    def clean(self) -> bool:
        return self.failure.clean and self.error_message is None

    def redact(self, text: str) -> str:
        return "Image prompt failed; backend diagnostics withheld." if self.sensitive else text

    def error(
        self, text: str, diagnostics: tuple[PiDiagnostic, ...] = (),
        *, source: FailureSource,
    ) -> events.Error:
        self.error_message = self.redact(text)
        return events.Error(
            text=self.error_message, diagnostics=() if self.sensitive else diagnostics,
            source=source.for_publication(self.error_message, self.sensitive),
        )

    def startup_error(self, stderr: str) -> None:
        if stderr.strip() and not self.sensitive:
            self.failure = self.failure.with_startup_diagnostics(stderr.strip())

    def done(self, session: NativeSessionCallbacks, stderr: str) -> Iterator[events.AgentEvent]:
        # Output checks turn permission and child ownership; it grants neither.
        transport_ok = not self.failure_text and (
            session.native_session.child_state.retained or session.native.proc.returncode == 0
        )
        session.prompt_state.validate_terminal(session, transport_ok)
        success = transport_ok and self.clean and session.inputs.settled
        terminal = events.Done(
            text=self.terminal_text(success, stderr, session.native.proc.returncode),
            ok=success and session.native.attestation.trustworthy,
            failure=self.declared_failure,
            retry_result=self.retry_result,
            diagnostic={
                **self.diagnostic,
                **(
                    {"exit_code": session.native.proc.returncode}
                    if session.native.proc.returncode is not None
                    else {}
                ),
            },
        )

        # Completion reports an outcome. Only this output owner can tell whether
        # its failure already produced an Error or first arose during settlement.
        if not terminal.ok and self.error_message is None:
            yield self.error(terminal.text, source=self._failed_message_source)
        yield terminal

    def permits_retention(self, session: NativeSessionCallbacks) -> bool:
        # Called only after the native lifecycle has proved the child idle. Apply
        # declaration-owned terminal evidence before handing the child back.
        session.prompt_state.validate_terminal(session, transport_ok=True)
        return self.clean

    def terminal_text(self, success: bool, stderr: str, returncode: int | None) -> str:
        def unfailed() -> str:
            if success:
                return "".join(self.text_parts).strip()
            return (
                self.error_message
                or (self.redact(stderr) if stderr else "")
                or f"Backend exited with code {returncode}"
            )

        return self.failure.terminal_text(unfailed)
