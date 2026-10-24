"""Inputs that reach a selected turn after its first one."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from .acp_extension import LiveQueueScope
from .agent_backend import TurnInbox
from .coordinator import Coordination
from .owned_send import OwnedSend
from agent_comms.goal_turn_permit import NoLaunchPermit
from .queued_input import DeliveredInput, InputHandoffRefused
from .thread_identity import ClaimedIdentity
from .turn_input_source import AcceptedFollowingInput

if TYPE_CHECKING:
    from .threads import Thread
    from .agent_events import InputRefused
    from .turn_runner import TurnRunner
    from .input_drain import InputDrain
    from .tracked_turn import TrackedTurnSession


class FollowingInputs:
    """A selected turn run outside an owner process: nothing can follow its first input."""

    @staticmethod
    async def ready(inputs: InputDrain, session_id: str, after_seq: int = 0) -> None:
        """Take accepted work when a running turn opens or frees following capacity."""
        async with inputs.state(session_id).lock:
            inputs.state(session_id).forget_observation()
            owner = await Coordination.run_worker(partial(
                inputs.comms.registry.require, inputs.sessions.require(session_id),
            ))
            await DeliveredInput.deliver_pending(
                inputs, session_id, owner, inputs.comms.bus,
                inputs.comms.owners.require_private_launch().wire_root_id, after_seq=after_seq,
            )

    async def open(self, session: TrackedTurnSession) -> None:
        return None

    async def started(self, input_id: str) -> AcceptedFollowingInput:
        raise AssertionError("A turn without an inbox cannot start a following input")

    async def retired(self, event: InputRefused) -> None:
        raise AssertionError("A turn without an inbox cannot retire a following input")


@dataclass
class OwnerFollowingInputs(FollowingInputs):
    """The owner's inbox for one selected turn, read by that turn once its first input is in."""

    inputs: InputDrain
    session_id: str
    owner: Thread
    inbox: TurnInbox
    scope: LiveQueueScope = field(init=False)

    async def pass_on_unread(self, turns: TurnRunner) -> None:
        """Inputs the ended turn never read start the next turn or are refused, never replayed."""
        for input_id in tuple(self.inputs.state(self.session_id).queued):
            if await self.pass_on(turns, input_id):
                return  # The next turn owns the remaining live acceptances.

    async def pass_on(self, turns: TurnRunner, input_id: str) -> bool:
        inputs, session_id = self.inputs, self.session_id
        item = inputs.state(session_id).queued.get(input_id)
        try:
            if item is None:
                raise InputHandoffRefused("The input was retired before its turn read it")
            await item.dispatch(turns, session_id, self.owner.name)
        except InputHandoffRefused:
            await inputs.input_refused(session_id, input_id)
            return False
        return True

    async def open(self, session: TrackedTurnSession) -> None:
        inputs = self.inputs
        thread = await Coordination.run_worker(partial(inputs.comms.registry.require, self.owner.name))
        lease = thread.require_turn_lease()
        original = session.prompt_send_boundary.following_original(session.command.message)
        sender = OwnedSend(
            comms=inputs.comms,
            owner_name=thread.name,
            inputs=inputs,
            goal_store=await Coordination.run_worker(inputs.comms.goals.open_goal_store),
            session_id=self.session_id,
            thread=thread,
            turn=lease.turn_id,
            claim_generation=lease.claimed_generation,
            original=original,
            goal_permit=NoLaunchPermit(),
        )
        self.scope = LiveQueueScope(
            session_id=self.session_id,
            claimed_identity=ClaimedIdentity(lease.identity.incarnation, lease.claimed_generation),
            owner=thread.require_process(),
        )
        inputs.state(self.session_id).capture_first_source(original)
        session.forward_inbox(self.inbox, sender)
        await self.ready(
            inputs, self.session_id,
            after_seq=session.prompt_send_boundary.following_after_seq,  # type: ignore[union-attr]
        )

    async def started(self, input_id: str) -> AcceptedFollowingInput:
        source = self.inputs.state(self.session_id).following[input_id]
        await source.started(self.inputs, self.session_id, self.scope)
        await self.ready(self.inputs, self.session_id)
        return source

    async def retired(self, event: InputRefused) -> None:
        await event.apply_to(self.inputs, self.session_id)
