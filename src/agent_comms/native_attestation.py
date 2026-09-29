"""Native capability and identity are observed states, never parallel turn flags."""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import pi_commands as commands
from . import turn_failure as failures
from .native_pi import CAPABILITY
from .native_session_reopen import NativeSessionIdentity
from .pi_events import PiEvent, Response
from .pi_payloads import StateData

if TYPE_CHECKING:
    from .backend import TurnSession


class AttestationError(ValueError):
    def __init__(self, failure: failures.TurnFailure):
        super().__init__(failure.text)
        self.failure = failure

    async def refuse(self, session: TurnSession) -> None:
        session.output.record_failure(self.failure)
        await session.native.proc.stop()


class IdentityAttestationError(AttestationError):
    async def refuse(self, session: TurnSession) -> None:
        session.native.attestation = session.native.attestation.invalidate()
        await super().refuse(session)


class NativeAttestation(ABC):
    @property
    @abstractmethod
    def observed(self) -> bool: ...

    uncertain = False
    state = None
    identity = None

    def observe(self, data: StateData) -> NativeAttestation:
        return self

    def invalidate(self) -> NativeAttestation:
        return LostAttestation()

    def conflicts(self, data) -> bool:
        return False


@dataclass
class PendingAttestation(NativeAttestation):
    observed = False
    expected: NativeSessionIdentity | None = None
    request: commands.GetState = field(
        default_factory=lambda: commands.GetState(
            id=f"agent-comms-preflight-{secrets.token_hex(16)}"
        )
    )

    def accept(self, event: PiEvent) -> ObservedAttestation:
        if not isinstance(event, Response) or event.command is not commands.GetState:
            raise AttestationError(
                failures.InputIdUnavailable(
                    "Pi native input-ID capability preflight returned another event."
                )
            )
        if event.id != self.request.id or not event.success:
            raise AttestationError(
                failures.InputIdUnavailable("Pi native input-ID capability preflight failed.")
            )
        if event.data is None or event.data.native_input_proof_capability != CAPABILITY:
            raise AttestationError(
                failures.InputIdUnavailable("Pi native input-ID capability preflight failed.")
            )
        observed = self.observe(event.data)
        if self.expected is not None and observed.identity != self.expected:
            raise IdentityAttestationError(
                failures.IdentityUncertain("Pi session identity changed during this turn.")
            )
        return observed

    def observe(self, data):
        return ObservedAttestation(data)


@dataclass
class ObservedAttestation(NativeAttestation):
    state: StateData
    observed = True

    @property
    def identity(self):
        if self.state.session_id and self.state.session_file:
            return NativeSessionIdentity(self.state.session_id, self.state.session_file)
        return None

    def conflicts(self, data):
        if self.state.session_id and data.session_id and self.state.session_id != data.session_id:
            return True
        return bool(
            self.state.session_file
            and data.session_file
            and self.state.session_file != data.session_file
        )


class LostAttestation(NativeAttestation):
    observed = False
    uncertain = True


class SavedSessionReopenError(ValueError):
    """Strict saved-session validation failed before a native process was started."""
