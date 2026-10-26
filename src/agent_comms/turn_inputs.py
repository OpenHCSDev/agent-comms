"""Input forwarding owns pending native IDs and never replays written inputs.

Declared handlers on this owner select each inbox operation by its nominal
MRO, and convert it to its Pi command at the owning send boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import agent_events as events
from . import pi_commands as commands
from . import turn_failure as failures
from .acp_failure import UnrecordedFailureSource
from .agent_backend import ClearQueue, InputRequest, SendNow, TurnInbox, TurnInboxItem, UseSettings
from .field_codec import InputId
from .locked_store import RegistryFile
from .maintenance_barrier import MaintenanceBarrier
from .mro_dispatch import MroDispatch, handles
from .native_failures import NativePiUnavailable
from .pending_requests import PendingRequest
from .prompt_state import PreparedSession, UnwrittenPrompt

if TYPE_CHECKING:
    from .backend import NativeSession, TurnSession
    from .pi_events import MessageStart, Response


@dataclass
class InputForwarding(MroDispatch):
    queue: TurnInbox = field(default_factory=TurnInbox)
    pending: dict[InputId, PendingRequest[commands.FollowingPrompt]] = field(default_factory=dict)
    current: TurnInboxItem | None = None
    started: bool = False
    uncertain: bool = False

    @property
    def permits_interaction(self) -> bool:
        """Observed authority loss blocks another native interaction."""
        return not self.uncertain

    @property
    def unresolved(self) -> bool:
        # Core's unwritten inbox is not work held by the native child.
        return bool(self.pending)

    @property
    def settled(self) -> bool:
        return not self.uncertain and not self.unresolved

    def retire(self, native_id: InputId) -> InputRequest:
        """Remove one exact native identity from the sole pending index."""
        return self.pending.pop(native_id).request.input

    def rejected(
        self, native_id: InputId, response: Response, session: TurnSession,
    ) -> tuple[events.AgentEvent, ...]:
        """Retire the live write, preserve its durable UNKNOWN, and report the actual refusal."""
        item = self.retire(native_id)
        failure = failures.PromptSendFailed(
            f"Following input {item.input_id} was rejected: {response.error.require()}"
        )
        session.output.record_failure(failure)
        session.rejected_commands.append(events.FollowingInputRefused(id=item.input_id))
        session.rejected_signal.set()
        return (session.output.error(failure.text, source=UnrecordedFailureSource()),)

    async def forward(self, session: NativeSession) -> None:
        # Inputs that wait through one tool batch reach the model together,
        # not one model call apiece.
        await self.write(session, commands.SetSteeringMode(mode="all"))
        while True:
            self.current = await self.queue.get()
            try:
                # An inbox item performs one operation, not every matching base handler.
                handler = next(self.handlers_for(self.current))
                if not await handler(self.current, session):
                    return
            finally:
                self.current = None

    @handles(TurnInboxItem)
    async def unhandled(self, item: TurnInboxItem, session: NativeSession) -> bool:
        raise TypeError(f"No input handler declared for {type(item).__name__}")

    @handles(UnwrittenPrompt)
    async def original_prompt(self, item: UnwrittenPrompt, session: TurnSession) -> bool:
        def write():
            session.grant_prompt()
            session.native.send(session.command)
        barrier = MaintenanceBarrier(RegistryFile.path_in(session.startup.root))
        async with barrier.ingress_async() as status:
            with status.phase.send_new(
                session.send_boundary.send_original,
                session.command.input_id, session.command.message,
            ) as outcome:
                completion = outcome.dispatch(self, session, item, write)
        return await completion

    @handles(PreparedSession)
    async def prepare_session(self, item: PreparedSession, session: NativeSession) -> bool:
        session.native.forget_context()
        state = session.native.attestation.require_state()
        identity = session.native.attestation.require_identity()
        session.launch.session.attest(identity)
        if (
            state.is_streaming.value_or(True)
            or state.is_compacting.value_or(True)
            or not state.pending_message_count.matches(0)
        ):
            raise NativePiUnavailable("Native preparation did not attest the idle saved session")
        session.active_session_file = identity.session_file
        await session.stats.request(session)
        return True

    async def write(self, session: NativeSession, *written: commands.PiCommand) -> None:
        for command in written:
            session.native.send(command)
        await session.native.drain()

    async def queued(self, input_ids: tuple[InputId, ...]) -> tuple[InputId, ...]:
        """A native prompt acknowledgement proves it reached the child's queue."""
        selected = frozenset(input_ids)
        candidates = {
            key: entry for key, entry in self.pending.items()
            if entry.request.input.input_id in selected
        }
        await asyncio.gather(*(entry.response() for entry in candidates.values()))
        return tuple(key for key in candidates if key in self.pending)

    @handles(ClearQueue)
    async def clear_queue(self, item: ClearQueue, session: TurnSession) -> bool:
        command = commands.ClearQueue(input_ids=await self.queued(item.input_ids))
        if not command.input_ids:
            return True
        await self.write(session, command)
        return True

    def cleared(
        self, requested: tuple[InputId, ...], response: Response, session: TurnSession,
    ) -> None:
        """The native reply owns removal, even after the forwarding task retires."""
        for native_id in response.data.require_payload().require_requested(requested):
            removed = self.retire(native_id)
            session.rejected_commands.append(events.InputCleared(
                id=removed.input_id, native_id=native_id, text=removed.content.text,
            ))
        session.rejected_signal.set()

    @handles(UseSettings)
    async def use_settings(self, item: UseSettings, session: TurnSession) -> bool:
        # Pi resets thinking to the new model's default, so the level follows the model.
        await self.write(
            session,
            item.model.set_model(commands.SetModel),
            *item.thinking_level.native_arguments(
                lambda level: commands.SetThinkingLevel(level=level.wire_value())
            ),
        )
        return True

    @handles(SendNow)
    async def send_now(self, item: SendNow, session: TurnSession) -> bool:
        candidates = await self.queued(item.input_ids)
        if not candidates:
            return True
        first = self.pending[candidates[0]].request.input
        async with MaintenanceBarrier(RegistryFile.path_in(session.startup.root)).ingress_async() as status:
            with status.phase.send_running(
                session.send_boundary.send_interrupt, first.input_id,
                candidates[0], first.content.text,
            ) as outcome:
                completion = outcome.dispatch(self, session, item, lambda: session.native.send(
                    commands.InterruptSteering(input_ids=list(candidates))
                ))
        return await completion

    @handles(InputRequest)
    async def place(self, item: InputRequest, session: TurnSession) -> bool:
        public_id = item.input_id
        native_id = InputId.mint()
        text = item.content.text
        command = commands.FollowingPrompt(
            input=item,
            input_id=native_id,
            message=text,
            images=item.content.images,
            context_contributions=item.content.contributions,
            owner_tools=session.tool_socket.listener.connection,
            streaming_behavior=item.when_busy.streaming(),
        )
        def write():
            if command.images:
                session.output.sensitive = True
            self.pending[native_id] = session.native.send(command)

        async with MaintenanceBarrier(RegistryFile.path_in(session.startup.root)).ingress_async() as status:
            with status.phase.send_running(
                session.send_boundary.send_following, public_id, native_id, text,
            ) as outcome:
                completion = outcome.dispatch(self, session, item, write)
        return await completion

    def refuse(self, session: TurnSession, input_ids: tuple[InputId, ...]) -> None:
        for input_id in input_ids:
            session.rejected_commands.append(events.FollowingInputRefused(id=input_id))
            session.rejected_signal.set()

    def mark_started(
        self, session: TurnSession, payload: MessageStart,
    ) -> tuple[InputId] | tuple[()]:
        message = payload.message
        if not message.user or not message.input_id.reported:
            return ()
        native_id = message.input_id.require()
        pending = self.pending.get(native_id)
        if pending is None or message.text != pending.request.input.content.text:
            return ()
        entry = pending.request.input
        public_id = entry.input_id
        if not session.input_start.following(public_id, native_id, entry.content.text):
            return ()
        self.retire(native_id)
        self.started = True
        return (public_id,)
