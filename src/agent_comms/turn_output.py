"""Output, diagnostic privacy and terminal failure selection for one native turn."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import agent_events as events
from . import turn_failure as failures
from .acp_failure import FailureSource, UnrecordedFailureSource, ProviderRetryResult
from .native_arguments import SelectedModel

if TYPE_CHECKING:
    from .backend import TurnSession
    from .pi_payloads import PiDiagnostic


@dataclass
class TurnOutput:
    sensitive: bool = False
    failure: failures.TurnFailure | None = None
    error_message: str | None = None
    final_assistant_stop: bool = False
    diagnostic: dict[str, int] = field(default_factory=dict)
    preflight_failure: str | None = None
    # The live turn owns the one privacy-safe decoded retry result. Completion
    # and failure presentation retain this immutable value; they do not decode it.
    retry_result: ProviderRetryResult | None = None
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
        self.discard_text()
        self.error_message = None
        self.final_assistant_stop = False

    def record_failure(self, failure: failures.TurnFailure) -> None:
        if failure.supersedes(self.failure):
            self.failure = failure

    def request_retried(self) -> None:
        """Pi re-sends the refused request; that refusal no longer decides the turn."""
        if self.failure is not None and self.failure.ends_with_request_retry:
            self.failure = None
        self.error_message = None

    def raise_if_failed(self, proof, model: SelectedModel) -> None:
        if self.failure is not None:
            self.declared_failure.raise_native(proof, model)

    @property
    def declared_failure(self) -> failures.TurnFailure:
        failure = self.failure
        if failure is None:
            raise ValueError("Failed native terminal lacks its declared failure")
        if self.retry_result is None:
            return failure
        return self.retry_result.refine_failure(failure)

    @property
    def failure_text(self) -> str:
        return self.failure.text if self.failure else ""

    @property
    def clean(self) -> bool:
        return self.failure is None and self.error_message is None

    def redact(self, text: str) -> str:
        return "Image prompt failed; backend diagnostics withheld." if self.sensitive else text

    def error(
        self, text: str, diagnostics: tuple[PiDiagnostic, ...] = (),
        source: FailureSource = UnrecordedFailureSource(),
    ) -> events.Error:
        self.error_message = self.redact(text)
        return events.Error(
            text=self.error_message, diagnostics=() if self.sensitive else diagnostics,
            source=source.for_publication(self.error_message, self.sensitive),
        )

    def startup_error(self, stderr: str) -> None:
        if self.failure and stderr.strip() and not self.sensitive:
            self.failure = self.failure.with_startup_diagnostics(stderr.strip())

    def done(self, session: TurnSession, stderr: str) -> Iterator[events.AgentEvent]:
        # Output checks turn permission and child ownership; it grants neither.
        transport_ok = not self.failure_text and (
            session.native_session.child_state.retained or session.native.proc.returncode == 0
        )
        self.settle(session, transport_ok)
        success = transport_ok and self.clean and session.inputs.settled
        terminal = events.Done(
            text=self.terminal_text(success, stderr, session.native.proc.returncode),
            ok=success and session.native.attestation.trustworthy,
            reason_code=self.failure.code if self.failure else None,
            retry_result=self.retry_result,
            diagnostic={
                **self.diagnostic,
                **({"reason": self.preflight_failure} if self.preflight_failure else {}),
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
            yield self.error(terminal.text)
        yield terminal

    def settle(self, session: TurnSession, transport_ok: bool) -> None:
        for cause in sorted(
            failures.TurnFailure.members_with(failures.TerminalFailure),
            key=lambda member: member.precedence,
            reverse=True,
        ):
            if cause.detected(session, transport_ok):
                self.record_failure(cause(cause.explanation(self)))

    def permits_retention(self, session: TurnSession) -> bool:
        # Called only after the native lifecycle has proved the child idle. Apply
        # declaration-owned terminal evidence before handing the child back.
        self.settle(session, transport_ok=True)
        return self.clean

    def terminal_text(self, success: bool, stderr: str, returncode: int | None) -> str:
        if self.failure is not None:
            return self.failure.text
        if success:
            return "".join(self.text_parts).strip()
        return (
            self.error_message
            or (self.redact(stderr) if stderr else "")
            or f"Backend exited with code {returncode}"
        )
