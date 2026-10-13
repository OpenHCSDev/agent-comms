"""Inputs that reach a selected turn after its first one."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from .acp_extension import QueueScope
from .agent_backend import TurnInboxItem
from .coordinator import Coordination
from .owned_send_admission import OwnedSendAdmission
from .thread_identity import AdmissionIdentity, TurnId
from .turn_input_source import SelectedOriginalInput

if TYPE_CHECKING:
    from .input_drain import InputDrain
    from .tracked_turn import TrackedTurnSession


class FollowingInputs:
    """A selected turn run outside an owner process: nothing can follow its first input."""

    async def open(self, session: TrackedTurnSession) -> None:
        return None

    async def started(self, input_id: str) -> None:
        raise AssertionError("A turn without an inbox cannot start a following input")

    async def refused(self, input_id: str | None) -> None:
        raise AssertionError("A turn without an inbox cannot refuse a following input")


@dataclass
class OwnerFollowingInputs(FollowingInputs):
    """The owner's inbox for one selected turn, read by that turn once its first input is in."""

    inputs: InputDrain
    session_id: str
    owner_name: str
    inbox: asyncio.Queue[TurnInboxItem]
    scope: QueueScope = field(init=False)

    async def open(self, session: TrackedTurnSession) -> None:
        inputs = self.inputs
        thread = await Coordination.run_worker(partial(inputs.comms.registry.require, self.owner_name))
        lease = thread.require_turn_lease()
        inputs.bind_native_turn(self.session_id, thread, lease.admission_generation, lease.turn_id)
        original = SelectedOriginalInput.answering(session.command.message)
        admission = OwnedSendAdmission(
            comms=inputs.comms,
            inputs=inputs,
            goal_store=await Coordination.run_worker(inputs.comms.goals.open_goal_store),
            session_id=self.session_id,
            thread=thread,
            turn=TurnId(lease.turn_id),
            admission=lease.admission_generation,
            original=original,
            goal_permit=None,
        )
        self.scope = QueueScope(
            self.session_id,
            AdmissionIdentity(lease.identity.incarnation, lease.admission_generation),
            thread.pid,
        )
        inputs.original_sources[self.session_id] = original
        session.forward_inbox(self.inbox, admission)

    async def started(self, input_id: str) -> None:
        await self.inputs.input_started(self.session_id, input_id, self.scope)

    async def refused(self, input_id: str | None) -> None:
        await self.inputs.input_refused(self.session_id, input_id)
