"""Native capability and identity are observed states, never parallel turn flags."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms.execution.native_failures import NativePiUnavailable
from agent_comms.execution.native_pi import CAPABILITY
from agent_comms.execution.native_session_reopen import (
    NativeSessionIdentity,
    NoSessionRetention,
    SessionRetention,
)
from agent_comms.execution.pi_commands import GetState
from agent_comms.execution.pi_events import PiEvent
from agent_comms.execution.pi_wire import RequestId
from agent_comms.execution.turn_failure import IdentityUncertain, InputIdUnavailable, TurnFailure
from agent_comms.execution.turn_item import PiRequestKey
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.pi_payloads import NativeSessionSnapshot, SnapshotAttestation, StateData

if TYPE_CHECKING:
    from agent_comms.execution.backend import TurnSession


class AttestationError(ValueError):
    def __init__(self, failure: TurnFailure):
        super().__init__(failure.text)
        self.failure = failure

    async def refuse(self, session: TurnSession) -> None:
        session.output.record_failure(self.failure)
        await session.native.shutdown()


class IdentityAttestationError(AttestationError):
    async def refuse(self, session: TurnSession) -> None:
        session.native.attestation = session.native.attestation.invalidate()
        await super().refuse(session)


class NativeAttestation(SnapshotAttestation):
    @property
    @abstractmethod
    def observed(self) -> bool: ...

    uncertain = False

    @property
    def trustworthy(self):
        return not self.uncertain

    state = None
    identity: SessionRetention = NoSessionRetention()

    def require_state(self) -> StateData:
        raise NativePiUnavailable("Native child has not reported its original state")

    @property
    def diagnostic_evidence(self):
        return {"attestation": "lost"}

    def observe(self, data: StateData) -> NativeAttestation:
        return self

    def invalidate(self) -> NativeAttestation:
        return LostAttestation()

    def allows_extension_input(self, inputs) -> bool:
        return self.identity.allows_extension_input(inputs)

    def conflicts(self, data: NativeSessionSnapshot) -> bool:
        return False


@dataclass
class PendingAttestation(NativeAttestation):
    observed = False
    expected: SessionRetention = field(default_factory=NoSessionRetention)
    request: GetState = field(
        default_factory=lambda: GetState(
            id=RequestId(PiRequestKey.fresh())
        )
    )

    @property
    def diagnostic_evidence(self):
        return {"control_command": self.request.to_rpc()}

    def accept(self, event: PiEvent) -> NativeAttestation:
        # Pi multiplexes events and independent replies on this channel. Only
        # the matching response may change whether this input can start.
        for response in event.responses_to(self.request):
            try:
                data = response.require_data().require_state()
            except (NativePiUnavailable, ValueError) as error:
                raise AttestationError(
                    InputIdUnavailable("Pi native input-ID capability preflight failed.")
                ) from error
            if data.native_input_proof_capability != CAPABILITY:
                raise AttestationError(
                    InputIdUnavailable("Pi native input-ID capability preflight failed.")
                )
            observed = ObservedAttestation(data)
            if self.expected.conflicts_with(observed.identity):
                raise IdentityAttestationError(
                    IdentityUncertain("Pi session identity changed during this turn.")
                )
            return observed
        return self


@dataclass(frozen=True)
class KnownSessionAttestation(NativeAttestation):
    """Core's verified saved-session identity for a tracked prompt."""

    identity: NativeSessionIdentity
    observed = True

    @property
    def diagnostic_evidence(self):
        return {"session_id": self.identity.session_id, "session_file": self.identity.session_file}


    def conflicts(self, data: NativeSessionSnapshot) -> bool:
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

    def require_state(self) -> StateData:
        return self.state

    @property
    def diagnostic_evidence(self):
        return {
            "session_id": FieldCodec.encode(self.state.session_id),
            "session_file": FieldCodec.encode(self.state.session_file),
        }

    @property
    def identity(self):
        return self.state.identity


    def conflicts(self, data: NativeSessionSnapshot) -> bool:
        return self.state.conflicts(data)


class LostAttestation(NativeAttestation):
    observed = False
    uncertain = True
