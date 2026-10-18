"""A reserved selected input owns error reporting through native cleanup and settlement."""

from __future__ import annotations

from asyncio import CancelledError
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial

from .coordination_errors import StaleFence
from .coordinator import Coordination
from .native_pi import NativePiTerminalFailure, NativeRequestFailure
from .private_send_claim import PrivateSendClaim
from .selected_participant import SelectedParticipant
from .selected_result import publish_native_failure
from .turn_context import RenderedInput


@dataclass(frozen=True)
class SelectedRequest:
    participant: SelectedParticipant
    claim_scope: PrivateSendClaim

    @classmethod
    async def reserve(cls, participant, session, stage, token, prompt: RenderedInput):
        return cls(
            participant,
            await PrivateSendClaim.reserve(
                selected=participant,
                stage=stage,
                token=token,
                prompt=prompt,
                session=session,
            ),
        )

    @asynccontextmanager
    async def native_failures(self):
        try:
            yield
        except NativePiTerminalFailure as error:
            # The current input operation has already confirmed the live failure
            # and settled this stage after raw writer, child and tool cleanup.
            await Coordination.run_worker(partial(publish_native_failure,
                self.participant, error.context.input_id, error.public_message, source_error=error
            ))
            raise
        except NativeRequestFailure as error:
            await self._native_failure(error)
            raise
        except CancelledError:
            # Native execution has unwound child/tool cleanup before the
            # reserved request sees cancellation of its abandoned result.
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
        await Coordination.run_worker(partial(publish_native_failure,
            self.participant,
            self.claim_scope.input_id,
            error.public_failure,
            native_response=error.rejected_response,
            source_error=error,
        ))
