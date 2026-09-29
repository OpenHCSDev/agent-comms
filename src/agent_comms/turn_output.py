"""Output, diagnostic privacy and terminal failure selection for one native turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import agent_events as events
from . import turn_failure as failures
from .diagnostics import FailureReason

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
    _text: list[str] = field(default_factory=list, repr=False)
    _message: list[str] = field(default_factory=list, repr=False)

    def append(self, delta: str) -> None:
        self._text.append(delta)
        self._message.append(delta)

    def start_message(self) -> None:
        self._message.clear()

    def matches_message(self, text: str) -> bool:
        return bool(text) and text == "".join(self._message)

    def discard_text(self) -> None:
        self._text.clear()

    def interrupted(self) -> None:
        self.discard_text()
        self.error_message = None
        self.final_assistant_stop = False

    def record_failure(self, failure: failures.TurnFailure) -> None:
        if failure.supersedes(self.failure):
            self.failure = failure

    @property
    def failure_text(self) -> str:
        return self.failure.text if self.failure else ""

    @property
    def clean(self) -> bool:
        return self.failure is None and self.error_message is None

    def redact(self, text: str) -> str:
        return "Image prompt failed; backend diagnostics withheld." if self.sensitive else text

    def error(self, text: str, diagnostics: tuple[PiDiagnostic, ...] = ()) -> events.Error:
        self.error_message = self.redact(text)
        return events.Error(
            text=self.error_message, diagnostics=() if self.sensitive else diagnostics
        )

    def startup_error(self, stderr: str) -> None:
        if (
            self.preflight_failure == FailureReason.PREFLIGHT_EXIT
            and stderr.strip()
            and not self.sensitive
        ):
            self.record_failure(
                failures.InputIdUnavailable(
                    "Pi native input-ID capability preflight ended before attestation. "
                    "The prompt was not sent. Backend startup reported:\n" + stderr.strip()
                )
            )

    def done(self, session: TurnSession, stderr: str) -> events.Done:
        # Read admission and custody from their actual owners; output never grants either.
        transport_ok = not self.failure_text and (
            session.retained or session.native.proc.returncode == 0
        )
        success = (
            transport_ok
            and self.error_message is None
            and session.initial_input_started
            and self.final_assistant_stop
            and not session.inputs.uncertain
            and not session.unresolved_inputs
        )
        self.settle(session, transport_ok)
        return events.Done(
            text=self.terminal_text(success, stderr, session.native.proc.returncode),
            ok=success and self.failure is None and not session.session_identity_uncertain,
            reason_code=self.failure.code if self.failure else None,
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

    def settle(self, session: TurnSession, transport_ok: bool) -> None:
        for cause in sorted(
            failures.TurnFailure.members_with(failures.TerminalFailure),
            key=lambda member: member.precedence,
            reverse=True,
        ):
            if cause.detected(session, transport_ok):
                self.record_failure(cause(cause.explanation(self)))

    def permits_retention(self, session: TurnSession) -> bool:
        # Called only after the native lifecycle has proved idle custody. Apply
        # declaration-owned terminal evidence before handing the child back.
        self.settle(session, transport_ok=True)
        return self.clean

    def terminal_text(self, success: bool, stderr: str, returncode: int | None) -> str:
        if self.failure is not None:
            return self.failure.text
        if success:
            return "".join(self._text).strip()
        return (
            self.error_message
            or (self.redact(stderr) if stderr else "")
            or f"Backend exited with code {returncode}"
        )
