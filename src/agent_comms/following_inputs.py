"""Inputs that reach a selected turn after its first one."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial

from agent_comms.acp_extension import LiveQueueScope
from agent_comms.agent_events import InputRefused
from agent_comms.coordination.coordinator import Coordination
from agent_comms.execution.following_input_contracts import (
    FollowingInputRetirement,
    FollowingTurnSource,
)
from agent_comms.execution.input_drain import InputDrain
from agent_comms.execution.owned_send import OwnedSend
from agent_comms.execution.queued_input import DeliveredInput, InputHandoffRefused
from agent_comms.execution.turn_inbox import TurnInbox
from agent_comms.execution.turn_input_source import AcceptedFollowingInput
from agent_comms.nominal.field_codec import AcpSessionId, InputId
from agent_comms.registry.thread_identity import ClaimedIdentity
from agent_comms.registry.threads import Thread


class FollowingInputs:
    """A selected turn run outside an owner process: nothing can follow its first input."""

    @staticmethod
    async def ready(inputs: InputDrain, session_id: AcpSessionId, after_seq: int = 0) -> None:
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

    async def open(self, session: FollowingTurnSource[OwnedSend]) -> None:
        return None

    async def started(self, input_id: InputId) -> AcceptedFollowingInput:
        raise AssertionError("A turn without an inbox cannot start a following input")

    async def retired(self, event: InputRefused) -> None:
        raise AssertionError("A turn without an inbox cannot retire a following input")


@dataclass
class OwnerFollowingInputs(FollowingInputs, FollowingInputRetirement):
    """The owner's inbox for one selected turn, read by that turn once its first input is in."""

    inputs: InputDrain
    session_id: AcpSessionId
    owner: Thread
    inbox: TurnInbox
    sender: OwnedSend = field(init=False)

    @property
    def scope(self) -> LiveQueueScope:
        lease = self.sender.thread.require_turn_lease()
        return LiveQueueScope(
            session_id=self.session_id,
            claimed_identity=ClaimedIdentity(lease.identity.incarnation, lease.claimed_generation),
            owner=self.sender.thread.require_process(),
        )

    async def pass_on_unread(self) -> None:
        """Inputs the ended turn never read start the next turn or are refused, never replayed."""
        turns = self.inputs.effects.turns
        for input_id in tuple(self.inputs.state(self.session_id).queued):
            inputs, session_id = self.inputs, self.session_id
            item = inputs.state(session_id).queued.get(input_id)
            try:
                if item is None:
                    raise InputHandoffRefused("The input was retired before its turn read it")
                await item.dispatch(turns, session_id, self.owner.name)
            except InputHandoffRefused:
                await inputs.input_refused(session_id, input_id)
            else:
                return  # The next turn owns the remaining live acceptances.

    async def open(self, session: FollowingTurnSource[OwnedSend]) -> None:
        inputs = self.inputs
        sender = await Coordination.run_worker(partial(
            session.prompt_send_boundary.following_sender,
            inputs, inputs.state(self.session_id).owned().identifier, session.command.message,
        ))
        self.sender = sender
        inputs.state(self.session_id).capture_first_source(sender.original_source)
        session.forward_inbox(self.inbox, sender)
        await self.ready(
            inputs, self.session_id,
            after_seq=session.prompt_send_boundary.following_after_seq,
        )

    async def started(self, input_id: InputId) -> AcceptedFollowingInput:
        source = self.inputs.state(self.session_id).following[input_id]
        await source.started(self.inputs, self.session_id, self.scope)
        await self.ready(self.inputs, self.session_id)
        return source

    async def retired(self, event: InputRefused) -> None:
        await event.apply_to(self.inputs, self.session_id)
