"""A reserved selected input owns error reporting through native cleanup and settlement."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from .coordination_errors import StaleFence
from .native_pi import NativePiTerminalFailure, NativePiUnavailable
from .private_send_admission import PrivateSendAdmission
from .selected_participant import SelectedParticipant
from .selected_result import publish_native_failure


@dataclass(frozen=True)
class SelectedRequest:
    participant: SelectedParticipant
    admission: PrivateSendAdmission

    @classmethod
    async def reserve(cls, participant, session, stage, token, prompt, *, package):
        # Every triage/FULL original crosses this same boundary, including
        # continuation after a prior triage changed the saved native context.
        await session.prepare_context(participant, package)
        return cls(
            participant,
            PrivateSendAdmission.reserve(
                selected=participant,
                stage=stage,
                token=token,
                prompt=prompt,
                session=session,
            ),
        )

    @contextmanager
    def native_failures(self):
        try:
            yield
        except NativePiTerminalFailure as error:
            # The existing admission has already corroborated the live failure
            # and settled this stage after raw writer, child and tool cleanup.
            publish_native_failure(
                self.participant, error.context.input_id, error.public_message, source_error=error
            )
            raise
        except NativePiUnavailable as error:
            self._native_failure(error)
            raise

    def _native_failure(self, error):
        try:
            self.admission.stage.fail_unknown(
                self.participant.bus,
                self.participant.owner,
                self.participant.response_owner,
            )
            self.participant.owner.require_registry(self.participant.comms.registry)
        except StaleFence:
            # A revoked owner cannot settle/publish for its successor. Recovery
            # retains the original UNKNOWN; this path never reconstructs input.
            return
        publish_native_failure(
            self.participant,
            self.admission.input_id,
            error.public_failure,
            native_response=error.rejected_response,
            source_error=error,
        )
