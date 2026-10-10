"""Input forwarding owns pending native IDs and never replays written inputs.

The turn's inbox carries Core's records (``InputRequest``, ``SendNow``). This
owner converts each to its Pi command when it writes it to the running child.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import agent_events as events
from . import pi_commands as commands
from . import turn_failure as failures
from .agent_backend import InputRequest, SendNow, WhenBusy

if TYPE_CHECKING:
    from .backend import TurnSession
    from .pi_events import MessageStart, Response


# Pi's spelling of each busy policy it supports; Pi has no way to refuse an input.
_PI_STREAMING_BEHAVIOR = {WhenBusy.STEER: "steer", WhenBusy.FOLLOW_UP: "followUp"}


@dataclass
class InputForwarding:
    queue: asyncio.Queue[InputRequest | SendNow] | None = None
    # (public input ID, sent text, native input ID) for each written, unstarted input.
    pending: list[tuple[str, str, str]] = field(default_factory=list)
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
    def settled(self) -> bool:
        return not self.uncertain and not self.unresolved

    def acknowledge(self, response: Response) -> None:
        if response.success is True and any(item[0] == response.id for item in self.pending):
            self.accepted.add(response.id)
        self.changed.set()

    async def forward(self, session: TurnSession) -> None:
        while await (await self.queue.get()).deliver_to(self, session):
            pass

    async def send_now(self, session: TurnSession, item: SendNow) -> bool:
        selected = {input_id.value for input_id in item.input_ids}
        while True:
            self.changed.clear()
            candidates = [entry for entry in self.pending if entry[0] in selected]
            if not candidates or all(entry[0] in self.accepted for entry in candidates):
                break
            await self.changed.wait()
        if not candidates:
            return True
        public_id, sent_text, native_id = candidates[0]
        from .backend import _maintenance_send_boundary

        boundary = _maintenance_send_boundary(
            session.startup.root,
            session.interrupt_boundary,
            public_id,
            native_id,
            sent_text,
        )
        async with boundary as authorized:
            if authorized:
                session.stdin.write(
                    session.native.reader.encode(
                        commands.InterruptSteering(input_ids=[entry[2] for entry in candidates])
                    )
                )
        if not authorized:
            self.uncertain = True
            session.output.final_assistant_stop = False
            session.output.record_failure(
                failures.AuthorityChanged("Input authority changed before immediate steering.")
            )
            await session.native.proc.stop()
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
        self.pending.append((public_id, text, native_id))
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
            await session.native.proc.stop()
            return False
        # None means never sent; preserve the owner's UNKNOWN, not a replay.
        self.pending[:] = [entry for entry in self.pending if entry[2] != native_id]
        session.rejected_commands.append(events.InputRefused(id=public_id))
        session.rejected_signal.set()
        return True

    def mark_started(self, session: TurnSession, payload: MessageStart) -> tuple[bool, str | None]:
        message = payload.message
        if not message.user:
            return False, None
        text = message.text
        native_id = message.input_id
        for index, (input_id, queued_text, expected_native_id) in enumerate(self.pending):
            if (
                (session.require_input_id or input_id in self.accepted)
                and text == queued_text
                and (not session.require_input_id or native_id == expected_native_id)
            ):
                if not session.notify_input_started(input_id, expected_native_id, queued_text):
                    return (False, None)
                self.accepted.discard(input_id)
                self.pending.pop(index)
                self.started = True
                return (True, input_id)
        return (False, None)
