"""Native capability and identity are observed states, never parallel turn flags."""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms.field_codec import FieldCodec

from . import pi_commands as commands
from . import turn_failure as failures
from .native_pi import CAPABILITY
from .native_session_reopen import NativeSessionIdentity
from .pi_events import PiEvent
from .pi_payloads import StateData

if TYPE_CHECKING:
    from .backend import TurnSession


class AttestationError(ValueError):
    def __init__(self, failure: failures.TurnFailure):
        super().__init__(failure.text)
        self.failure = failure

    async def refuse(self, session: TurnSession) -> None:
        session.output.record_failure(self.failure)
        await session.native.shutdown()


class IdentityAttestationError(AttestationError):
    async def refuse(self, session: TurnSession) -> None:
        session.native.attestation = session.native.attestation.invalidate()
        await super().refuse(session)


class NativeAttestation(ABC):
    @property
    @abstractmethod
    def observed(self) -> bool: ...

    uncertain = False

    @property
    def trustworthy(self):
        return not self.uncertain

    state = None
    identity = None

    def require_identity(self) -> NativeSessionIdentity:
        raise ValueError("Native child has not attested an original session identity")

    @property
    def diagnostic_evidence(self):
        return {"attestation": "lost"}

    def observe(self, data: StateData) -> NativeAttestation:
        return self

    def invalidate(self) -> NativeAttestation:
        return LostAttestation()

    def allows_extension_input(self, inputs) -> bool:
        return False

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

    @property
    def diagnostic_evidence(self):
        return {"control_command": self.request.to_rpc()}

    def accept(self, event: PiEvent) -> NativeAttestation:
        # Pi multiplexes events and independent replies on this channel. Only
        # the matching response may change whether this input can start.
        if not event.responds_to(self.request):
            return self
        if not event.succeeded:
            raise AttestationError(
                failures.InputIdUnavailable("Pi native input-ID capability preflight failed.")
            )
        try:
            data = event.data.require_payload()
        except ValueError as error:
            raise AttestationError(
                failures.InputIdUnavailable("Pi native input-ID capability preflight failed.")
            ) from error
        if data.native_input_proof_capability != CAPABILITY:
            raise AttestationError(
                failures.InputIdUnavailable("Pi native input-ID capability preflight failed.")
            )
        observed = ObservedAttestation(data)
        if self.expected is not None and observed.identity != self.expected:
            raise IdentityAttestationError(
                failures.IdentityUncertain("Pi session identity changed during this turn.")
            )
        return observed


@dataclass(frozen=True)
class KnownSessionAttestation(NativeAttestation):
    """Core's verified saved-session identity for a tracked prompt."""

    identity: NativeSessionIdentity
    observed = True

    @property
    def diagnostic_evidence(self):
        return {"session_id": self.identity.session_id, "session_file": self.identity.session_file}

    def require_identity(self) -> NativeSessionIdentity:
        return self.identity

    def allows_extension_input(self, inputs) -> bool:
        return inputs.permits_interaction

    def conflicts(self, data) -> bool:
        """Reject every reported component that differs from the known identity."""
        return any(
            observed != expected
            for expected, reported in (
                (self.identity.session_id, data.session_id),
                (self.identity.session_file, data.session_file),
            )
            for observed in reported.present()
        )


@dataclass
class ObservedAttestation(NativeAttestation):
    state: StateData
    observed = True

    @property
    def diagnostic_evidence(self):
        return {
            "session_id": FieldCodec.encode(self.state.session_id),
            "session_file": FieldCodec.encode(self.state.session_file),
        }

    @property
    def identity(self):
        return self.state.identity

    def require_identity(self) -> NativeSessionIdentity:
        identity = self.state.identity
        if identity is None:
            return super().require_identity()
        return identity

    def allows_extension_input(self, inputs) -> bool:
        return self.identity is not None and inputs.permits_interaction

    def conflicts(self, data):
        return self.state.conflicts(data)


class LostAttestation(NativeAttestation):
    observed = False
    uncertain = True


