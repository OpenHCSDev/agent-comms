from __future__ import annotations

from asyncio import CancelledError
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial

from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import StaleFence
from agent_comms.execution.drain_failures import UNAVAILABLE_DRAIN_ERRORS
from agent_comms.execution.native_failures import NativeRequestFailure
from agent_comms.execution.selected.participant import SelectedParticipant
from agent_comms.execution.selected.result import publish_native_failure
from agent_comms.execution.turn_context import RenderedInput
from agent_comms.execution.turn_failure import NativePiTerminalFailure
from agent_comms.private_send_claim import PrivateSendClaim


@dataclass(frozen=True)
class SelectedRequest:
    participant: SelectedParticipant
    claim_scope: PrivateSendClaim

    @classmethod
    async def reserve(cls, participant, session, stage, token, prompt: RenderedInput,
                      *, attempt: int = 0):
        return cls(
            participant,
            await PrivateSendClaim.reserve(
                selected=participant,
                stage=stage,
                token=token,
                prompt=prompt,
                session=session,
                attempt=attempt,
            ),
        )

    @asynccontextmanager
    async def native_failures(self):
        try:
            yield
        except NativePiTerminalFailure as error:
            # The current input operation has already confirmed the live failure
            # and settled this stage after raw writer, child and tool cleanup.
            sent = await Coordination.run_worker(partial(publish_native_failure,
                self.participant, error.context.input_id, error.public_message, source_error=error
            ))
            for notice in sent.notices:
                error.add_note(notice)
            raise
        except NativeRequestFailure as error:
            await self._native_failure(error)
            raise
        except (CancelledError, *UNAVAILABLE_DRAIN_ERRORS):
            # After native cleanup, cancellation or an unavailable read leaves
            # this abandoned request UNKNOWN; settlement errors propagate.
            await self._settle_unknown()
            raise

    async def _settle_unknown(self) -> bool:
        try:
            await self.claim_scope.stage.fail_unknown(
                self.participant.bus,
                self.participant.owner,
                self.participant.response_owner,
            )
            await Coordination.run_worker(partial(
                self.participant.owner.require_registry, self.participant.comms.registry
            ))
        except StaleFence:
            # A revoked owner cannot settle/publish for its successor. Recovery
            # retains the original UNKNOWN; this path never reconstructs input.
            return False
        return True

    async def _native_failure(self, error):
        if not await self._settle_unknown():
            return
        sent = await Coordination.run_worker(partial(error.publish,
            partial(publish_native_failure, self.participant, self.claim_scope.input_id),
        ))
        for notice in sent.notices:
            error.add_note(notice)
