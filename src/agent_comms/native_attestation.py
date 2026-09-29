"""The correlated native capability response is the attestation, never a flag."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from . import pi_commands as commands
from . import turn_failure as failures
from .native_pi import CAPABILITY
from .native_session_reopen import NativeSessionIdentity
from .pi_events import PiEvent, Response
from .pi_payloads import StateData


class AttestationError(ValueError):
    def __init__(self, failure: failures.TurnFailure):
        super().__init__(failure.text)
        self.failure = failure


@dataclass
class NativeAttestation:
    expected: NativeSessionIdentity | None = None
    request: commands.GetState = field(
        default_factory=lambda: commands.GetState(
            id=f"agent-comms-preflight-{secrets.token_hex(16)}"
        )
    )
    state: StateData | None = None

    def accept(self, event: PiEvent) -> None:
        if not isinstance(event, Response) or event.command is not commands.GetState:
            raise AttestationError(
                failures.InputIdUnavailable(
                    "Pi native input-ID capability preflight returned another event."
                )
            )
        state = event.data
        if (
            event.id != self.request.id
            or event.success is not True
            or state is None
            or state.native_input_proof_capability != CAPABILITY
        ):
            raise AttestationError(
                failures.InputIdUnavailable("Pi native input-ID capability preflight failed.")
            )
        if self.expected is not None and (
            state.session_id != self.expected.session_id
            or state.session_file != self.expected.session_file
        ):
            raise AttestationError(
                failures.IdentityUncertain("Pi session identity changed during this turn.")
            )
        self.state = state


class SavedSessionReopenError(ValueError):
    """Strict saved-session validation failed before a native process was started."""
