"""Input forwarding owns pending native IDs and never replays written inputs.

Each inbox member delivers itself to this receiver. This owner converts it to
its Pi command when it writes it to the running child.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field, replace
from uuid import uuid4
from typing import TYPE_CHECKING

from . import agent_events as events
from . import pi_commands as commands
from . import turn_failure as failures
from .agent_backend import ClearQueue, InputId, InputRequest, SendNow, UseSettings, WhenBusy, TurnInboxItem

if TYPE_CHECKING:
    from .backend import TurnSession
    from .pi_events import MessageStart, Response


# Pi's spelling of each busy policy it supports; Pi has no way to refuse an input.
_PI_STREAMING_BEHAVIOR = {WhenBusy.STEER: "steer", WhenBusy.FOLLOW_UP: "followUp"}


@dataclass(frozen=True)
class PendingNativeInput:
    input_id: InputId
    text: str
    native_id: str

    def cleared(self) -> events.InputCleared:
        return events.InputCleared(id=self.input_id.value, native_id=self.native_id, text=self.text)


@dataclass
class InputForwarding:
    queue: asyncio.Queue[TurnInboxItem] | None = None
    pending: dict[str, PendingNativeInput] = field(default_factory=dict)
    accepted: set[str] = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    generation: int = 0
    started: bool = False
    uncertain: bool = False

    @property
    def permits_admission(self) -> bool:
        """Only confirmed input delivery permits a fresh native interaction."""
        return not self.uncertain

    def can_forward(self, writer):
        return self.queue is not None and writer is not None

    @property
    def unresolved(self) -> bool:
        return bool(self.pending) or (self.queue is not None and not self.queue.empty())

    @property
    def all_written_started(self) -> bool:
        """The child holds no input it has not started yet."""
        return not self.pending

    @property
    def settled(self) -> bool:
        return not self.uncertain and not self.unresolved

    def acknowledge(self, response: Response) -> None:
        if response.success is True and any(item.input_id.value == response.id for item in self.pending.values()):
            self.accepted.add(response.id)
        self.changed.set()

    def retire(self, native_id: str) -> PendingNativeInput:
        """Remove one exact native identity from the sole pending index."""
        item = self.pending.pop(native_id)
        self.accepted.discard(item.input_id.value)
        self.changed.set()
        return item

    async def forward(self, session: TurnSession) -> None:
        # Inputs that wait through one tool batch reach the model together,
        # not one model call apiece.
        await self.write(session, commands.SetSteeringMode(mode="all"))
        while await (await self.queue.get()).deliver_to(self, session):
            pass

    async def write(self, session: TurnSession, *written: commands.PiCommand) -> None:
        for command in written:
            session.stdin.write(session.native.reader.encode(replace(command, id=uuid4().hex)))
        await session.stdin.drain()

    async def queued(self, input_ids: tuple[InputId, ...]) -> tuple[PendingNativeInput, ...]:
        """A native prompt acknowledgement proves it reached the child's queue."""
        selected = frozenset(input_id.value for input_id in input_ids)
        while True:
            self.changed.clear()
            candidates = tuple(entry for entry in self.pending.values() if entry.input_id.value in selected)
            if not candidates or all(entry.input_id.value in self.accepted for entry in candidates):
                return candidates
            await self.changed.wait()

    async def clear_queue(self, session: TurnSession, item: ClearQueue) -> bool:
        command = commands.ClearQueue(input_ids=tuple(
            entry.native_id for entry in await self.queued(item.input_ids)
        ))
        if not command.input_ids:
            return True
        async with command.pending_response(session.native.reader, session.stdin) as future:
            response = await future
        if not response.success:
            raise RuntimeError(f"Native queue clear failed: {response.error}")
        for native_id in response.data.require_payload().require_requested(command.input_ids):
            session.rejected_commands.append(self.retire(native_id).cleared())
        session.rejected_signal.set()
        return True

    async def use_settings(self, session: TurnSession, item: UseSettings) -> bool:
        provider, model_id = item.model.split("/", 1)
        # Pi resets thinking to the new model's default, so the level follows the model.
        await self.write(
            session,
            commands.SetModel(provider=provider, model_id=model_id),
            commands.SetThinkingLevel(level=item.thinking_level),
        )
        return True

    async def send_now(self, session: TurnSession, item: SendNow) -> bool:
        candidates = await self.queued(item.input_ids)
        if not candidates:
            return True
        first = candidates[0]
        from .backend import _maintenance_send_boundary

        boundary = _maintenance_send_boundary(
            session.startup.root,
            session.interrupt_boundary,
            first.input_id.value,
            first.native_id,
            first.text,
        )
        async with boundary as authorized:
            if authorized:
                session.stdin.write(
                    session.native.reader.encode(
                        commands.InterruptSteering(input_ids=[entry.native_id for entry in candidates])
                    )
                )
        if not authorized:
            self.uncertain = True
            session.output.final_assistant_stop = False
            session.output.record_failure(
                failures.AuthorityChanged("Input authority changed before immediate steering.")
            )
            return False
        await session.stdin.drain()
        return True

    async def place(self, session: TurnSession, item: InputRequest) -> bool:
        from .backend import _maintenance_send_boundary

        public_id = item.input_id.value
        native_id = secrets.token_hex(16)
        text = item.content.text
        if not session.require_input_id:
            text = f"[agent-comms input-id: {public_id}]\n{text}"
        command = commands.Prompt(
            id=public_id,
            input_id=native_id,
            message=text,
            images=item.content.images or None,
            context_contributions=item.content.contributions,
            streaming_behavior=_PI_STREAMING_BEHAVIOR[item.when_busy],
        )
        self.pending[native_id] = PendingNativeInput(item.input_id, text, native_id)
        boundary = _maintenance_send_boundary(
            session.startup.root,
            session.send_boundary,
            public_id,
            native_id,
            text,
        )
        async with boundary as authorized:
            if authorized:
                if command.images:
                    session.output.sensitive = True
                self.generation += 1
                session.stdin.write(session.native.reader.encode(command))
        if authorized:
            await session.stdin.drain()
            return True
        if authorized is False:
            self.uncertain = True
            session.output.final_assistant_stop = False
            session.output.record_failure(
                failures.AuthorityChanged("Input authority changed before Pi prompt send.")
            )
            return False
        # None means never sent; preserve the owner's UNKNOWN, not a replay.
        self.retire(native_id)
        session.rejected_commands.append(events.InputRefused(id=public_id))
        session.rejected_signal.set()
        return True

    def mark_started(self, session: TurnSession, payload: MessageStart) -> tuple[bool, str | None]:
        message = payload.message
        if not message.user:
            return False, None
        text = message.text
        native_id = message.input_id
        for entry in self.pending.values():
            public_id = entry.input_id.value
            if (
                (session.require_input_id or public_id in self.accepted)
                and text == entry.text
                and (not session.require_input_id or native_id == entry.native_id)
            ):
                if not session.notify_input_started(public_id, entry.native_id, entry.text):
                    return (False, None)
                self.retire(entry.native_id)
                self.started = True
                return (True, public_id)
        return (False, None)
