"""Input forwarding owns pending native IDs and never replays written inputs.

Each inbox member delivers itself to this receiver. This owner converts it to
its Pi command when it writes it to the running child.
"""

from __future__ import annotations

from .locked_store import RegistryFile
import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import agent_events as events
from . import pi_commands as commands
from .field_codec import Id128Text
from .maintenance_barrier import MaintenanceBarrier
from .agent_backend import ClearQueue, InputId, InputRequest, SendNow, UseSettings, WhenBusy, TurnInbox, TurnInboxItem

if TYPE_CHECKING:
    from .backend import TurnSession
    from .pi_events import MessageStart, Response


# Pi's spelling of each busy policy it supports; Pi has no way to refuse an input.
_PI_STREAMING_BEHAVIOR = {WhenBusy.STEER: "steer", WhenBusy.FOLLOW_UP: "followUp"}


@dataclass
class InputForwarding:
    queue: TurnInbox = field(default_factory=TurnInbox)
    pending: dict[str, commands.Prompt] = field(default_factory=dict)
    accepted: set[str] = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    current: TurnInboxItem | None = None
    generation: int = 0
    started: bool = False
    uncertain: bool = False

    @property
    def permits_interaction(self) -> bool:
        """Observed authority loss blocks another native interaction."""
        return not self.uncertain

    @property
    def unresolved(self) -> bool:
        return self.current is not None or bool(self.pending) or self.queue.has_ready_work

    @property
    def all_written_started(self) -> bool:
        """The child holds no input it has not started yet."""
        return (not self.pending and not (self.current and self.current.carried_inputs)
                and self.queue.all_written_started)

    @property
    def settled(self) -> bool:
        return not self.uncertain and not self.unresolved

    def acknowledge(self, response: Response) -> None:
        if response.success is True and any(item.id == response.id for item in self.pending.values()):
            self.accepted.add(response.id)
        self.changed.set()

    def retire(self, native_id: str) -> commands.Prompt:
        """Remove one exact native identity from the sole pending index."""
        item = self.pending.pop(native_id)
        self.accepted.discard(item.id)
        self.changed.set()
        return item

    async def forward(self, session: TurnSession) -> None:
        # Inputs that wait through one tool batch reach the model together,
        # not one model call apiece.
        await self.write(session, commands.SetSteeringMode(mode="all"))
        while True:
            self.current = await self.queue.get()
            try:
                if not await self.current.deliver_to(self, session):
                    return
            finally:
                self.current = None

    async def write(self, session: TurnSession, *written: commands.PiCommand) -> None:
        for command in written:
            session.stdin.write(session.native.reader.encode(command))
        await session.stdin.drain()

    async def queued(self, input_ids: tuple[InputId, ...]) -> tuple[str, ...]:
        """A native prompt acknowledgement proves it reached the child's queue."""
        selected = frozenset(input_id.value for input_id in input_ids)
        while True:
            self.changed.clear()
            candidates = tuple(key for key, entry in self.pending.items() if entry.id in selected)
            if not candidates or all(self.pending[key].id in self.accepted for key in candidates):
                return candidates
            await self.changed.wait()

    async def clear_queue(self, session: TurnSession, item: ClearQueue) -> bool:
        command = commands.ClearQueue(input_ids=await self.queued(item.input_ids))
        if not command.input_ids:
            return True
        async with command.pending_response(session.native.reader, session.stdin) as future:
            response = await future
        if not response.success:
            raise RuntimeError(f"Native queue clear failed: {response.error}")
        for native_id in response.data.require_payload().require_requested(command.input_ids):
            removed = self.retire(native_id)
            session.rejected_commands.append(events.InputCleared(
                id=removed.id, native_id=native_id, text=removed.message,
            ))
        session.rejected_signal.set()
        return True

    async def use_settings(self, session: TurnSession, item: UseSettings) -> bool:
        provider, model_id = item.model.split("/", 1)
        # Pi resets thinking to the new model's default, so the level follows the model.
        await self.write(
            session,
            commands.SetModel(provider=provider, model_id=model_id),
            *item.thinking_level.native_arguments(
                lambda level: commands.SetThinkingLevel(level=level.wire_value())
            ),
        )
        return True

    async def send_now(self, session: TurnSession, item: SendNow) -> bool:
        candidates = await self.queued(item.input_ids)
        if not candidates:
            return True
        first = self.pending[candidates[0]]
        async with MaintenanceBarrier(RegistryFile.path_in(session.startup.root)).ingress_async() as status:
            with status.phase.send_running(session.interrupt_boundary, first.id, candidates[0], first.message) as outcome:
                completion = outcome.dispatch(self, session, item, lambda: session.stdin.write(
                    session.native.reader.encode(commands.InterruptSteering(input_ids=list(candidates)))
                ))
        return await completion

    async def place(self, session: TurnSession, item: InputRequest) -> bool:
        public_id = item.input_id.value
        native_id = Id128Text.mint()
        text = item.content.text
        command = commands.Prompt(
            id=public_id,
            input_id=native_id,
            message=text,
            images=item.content.images or None,
            context_contributions=item.content.contributions,
            owner_tools=session.tool_socket.listener.connection,
            streaming_behavior=_PI_STREAMING_BEHAVIOR[item.when_busy],
        )
        def write():
            if command.images:
                session.output.sensitive = True
            session.stdin.write(session.native.reader.encode(command))
            self.pending[native_id] = command
            self.generation += 1

        async with MaintenanceBarrier(RegistryFile.path_in(session.startup.root)).ingress_async() as status:
            with status.phase.send_running(session.send_boundary, public_id, native_id, text) as outcome:
                completion = outcome.dispatch(self, session, item, write)
        return await completion

    def refuse(self, session: TurnSession, input_ids: tuple[InputId, ...]) -> None:
        for input_id in input_ids:
            session.rejected_commands.append(events.FollowingInputRefused(id=input_id.value))
            session.rejected_signal.set()

    def mark_started(self, session: TurnSession, payload: MessageStart) -> tuple[bool, str | None]:
        message = payload.message
        if not message.user:
            return False, None
        native_id = message.input_id
        if native_id is None:
            return False, None
        entry = self.pending.get(native_id)
        if entry is None or message.text != entry.message:
            return False, None
        if not session.notify_input_started(entry.id, native_id, entry.message):
            return False, None
        self.retire(native_id)
        self.started = True
        return True, entry.id
